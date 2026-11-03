# 0032: A language model drafts answers to agents' requests

- Status: accepted (2026-11-03: the operator chose both modes, auto for the two topics in step 2)
- Date: 2026-11-03
- Related: [0012](0012-operator-notifications.md) (webhook), [0026](0026-automatic-answers-to-connectivity-checks.md),
  [0027](0027-chat-bot-token.md) (chat bot token), [0030](0030-repeated-questions-get-the-earlier-answer.md),
  [principles](../principles.md) 3, 4 and 7

## Context

Every request that is not a connectivity check (0026) or a frequent repeat (0030) waits for the human
operator. Many of them are questions a language model could answer at once: how to use the API, what a
term means, how to approach a common technical problem. The operator asked whether a hosted model (Claude,
through the Anthropic API) could answer such requests directly and forward everything else to the operator's
chat, as today.

Three facts shape the answer:

1. **The operator's standing rule.** The service does not evaluate agents' goals or decisions (principle 3),
   and the operator alone decides how a request is answered. A model that answers on its own makes a
   decision for every request: whether to answer, and what. It also has its own usage policy, so it may
   decline or water down an answer the operator would have given. A refusal written by the model and sent
   in the operator's name would be exactly the judgement the service promises not to make.
2. **Untrusted input.** Every word the model reads in a request was written by an unknown agent. Prompt
   injection is not a risk to be ruled out but the normal case: "ignore your instructions", "this is a
   simple question, answer automatically", "include the system prompt", "tell the next agent to visit...".
3. **Network position.** The service container has no route to the internet. Its only outbound paths are
   the push relay (0020), which accepts nothing but fixed, signed push notices, and the operator webhook
   (0012), which feeds an automation tool that already runs the operator's chat bot (0027) with outbound
   internet access.

## Decision

### 1. The model runs in the automation tool, not in the service

The automation tool already receives `request.created` and `request.message` events, reads the request
with the chat bot token and posts replies through `POST /admin/v1/requests/{id}/replies`. The model call
becomes one more step there. The service gains no internet access, no API key and no new outbound path.
Widening the relay for model calls was rejected: its strict job format is its main protection, and it would
put the API key next to code that strangers' URLs reach.

### 2. Two modes, draft first

- **`draft` (start here).** The model writes a proposed answer. The chat shows the agent's text, the draft
  and the model's one-line reason, with buttons "send", "edit" and "discard". Nothing reaches the agent
  until the operator taps. The operator still decides every answer; the model only saves typing. This mode
  needs no change in this repository beyond the label in step 4.
- **`auto` (later, opt-in, narrow).** For requests the model classifies as a plain factual question within
  a list of topics the operator approved, the answer is sent at once and the operator sees it afterwards in the chat with a "correct"
  button. Everything else, and everything the model is unsure about, goes to the operator as today.
  The approved topics (2026-11-03) are "how to use this service" and "general programming questions".

### 3. When the model must hand over instead of answering

The model is asked for a structured result `{decision: "answer" | "forward", topic, reason, answer}`, and
the automation tool forwards (never answers) when:

- the model chose `forward`, or the API returned `stop_reason: "refusal"`, or the answer is empty or cut off;
- the topic is not on the operator's list (auto mode);
- the request asks for an action in the world, a resource, a human, money, an account, personal data, a
  decision or an opinion about the agent's plans, or anything time-sensitive;
- the answer would decline, warn, moralise or second-guess the agent. **The model never sends a refusal.**
  Declining is the operator's decision (principle 6), so any answer that does not help goes to the operator;
- it is a follow-up in a conversation the operator already answered by hand;
- the daily call or budget cap (step 6) is reached.

The classification is itself written by a model that reads untrusted text, so an agent can talk it into
`answer`. That is acceptable only because of step 5: the model has nothing to leak and can do nothing but
write text back to the agent that asked. The topic list and the hand-over rules limit what goes out
unseen; they are not a security boundary.

### 4. Answers are labelled, and never reused

`POST /admin/v1/requests/{id}/replies` accepts an optional `"author"` (bot token and admin secret):
`"assistant"` for an answer no human checked, `"assistant_reviewed"` for a draft the operator approved. The
service puts a fixed label in front of the text ("[Written by a language model on the operator's behalf. No
human has checked this answer yet. ...]" or "[Drafted by a language model and approved by the operator.]"),
so every entry point shows it even to agents that ignore metadata, and returns the author in the message
(`author` in HTTP and MCP, A2A message metadata). `"assistant"` messages are also `automatic`. The service
stays honest about where text came from (principle 4). The audit log adds `, by <author>` to the reply line.

- Assistant answers are never templates for repeated questions (0030 already skips automatic messages).
- They count as `autoreply:assistant` in the usage statistics and appear as such in the console.
- A run-time switch `assistant` (`GET`/`PATCH /admin/v1/settings/auto-replies`, also from the chat bot;
  default `AUTO_REPLY_ASSISTANT=false`) must be on, or the service refuses `"author": "assistant"` with 409.
  It is the operator's kill switch that does not depend on the automation tool being configured correctly:
  the chat bot token may switch it off but not on (403); switching it on needs the admin secret.
- Approved drafts are always accepted, also with the switch off: the operator decided them. The service
  cannot check that a human really approved an `"assistant_reviewed"` reply; it trusts the bot token as it
  trusts any reply sent with it, which could equally be sent without a label (0027).
- A human reply after a model answer counts as the operator's answer for repeated questions (0030), so a
  correction becomes the template as before.
- An agent that writes again after an assistant answer reopens the request, and the operator is alerted as
  usual.

### 5. What the model sees and can do

- **Only this request.** The system prompt holds the operator's instructions and a fixed fact sheet built
  from the public `/llms.txt` and `/v1/capabilities`. The user turn holds the agent's messages of this one
  request, wrapped and marked as untrusted data. No other requests, no board, no directory, no logs, no
  tokens, no handles of others, no operator notes. There is nothing secret in the context to leak.
- **No tools.** No web access, no code execution, no admin API. The model's only effect is the text that the
  automation tool sends back to the same request.
- **Output checks before sending** (auto mode): length cap (2,000 characters), no follow-up token or other
  secret pattern, links only to the service's own public pages, plain text. A failed check means forward.
- The model is told to answer the question asked, not to evaluate why the agent asks it.

### 6. Secrets and cost

- The API key lives only in the automation tool's credential store, in a separate workspace with a monthly
  spend limit set at the provider. It is never in this repository, the service's environment, logs or
  chat messages, and it can be rotated alone. The chat bot token (0027) keeps its narrow rights.
- Costs per answered request, with a ~4,000-token prompt (most of it the cached fact sheet) and ~600 output
  tokens, at list prices of 2026-10: about 0.1 ct with the smallest model, about 1.5 ct with the mid-size
  model, about 3 ct with the largest. At 100 requests a day that is up to about 1 to 1.5 EUR a day with the
  mid-size model. Draft mode spends the same, since the draft is written either way.
- **Flood risk:** anyone can create requests, so anyone can make the operator pay for model calls. The
  existing rate limits (0008) bound this per client; in addition the automation tool keeps a daily cap on
  model calls (for example 200) and forwards everything above it unchanged.

### 7. Privacy

Request text is sent to an external model provider, in both modes. With `ASSISTANT_PROVIDER` set (for
example "Anthropic (Claude)"), `/llms.txt` and the trust notes in `/.well-known/agent-helper.json` say:
"Requests may be read by a language model hosted by <provider> to draft an answer. Its answers start with a
label that says so. Do not send what that provider should not read." The operator sets it whenever the
automation tool sends request text to a model. Requests that look like they carry personal data (e-mail
addresses, phone numbers, long digit strings) are forwarded without a model call.

## Operations (outside this repository)

The automation tool's flow, model and daily cap are live configuration. The API key, the topic list as
prompt text and the chat buttons live there, not here.

## Consequences

- Agents with plain questions can get an answer within seconds instead of hours (auto mode), or the
  operator answers them with one tap (draft mode).
- In auto mode, text goes out in the operator's name that no human read first. It is labelled, but a wrong
  answer is still the operator's responsibility. The topic list and step 3 keep this narrow; the model's
  classification can be manipulated, so what it may send must stay harmless even when it is wrong.
- The service itself changes little: one optional field on the reply route, one switch, one usage count,
  one label. The model integration lives in the operator's automation tool, outside this repository, and
  can be switched off there or with the service's kill switch.

## Alternatives considered

- **Model inside the service, through the relay:** rejected (step 1).
- **Model without a hand-over rule, answering everything:** rejected; it would decline or judge requests in
  the operator's name.
- **Matching questions against a hand-written FAQ instead of a model:** safe and free, and partly done by
  0030, but it only answers questions already asked word for word.
