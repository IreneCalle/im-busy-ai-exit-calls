# Providers

I'm Busy uses four outside services. Only Anthropic is required: with `ANTHROPIC_API_KEY` alone you get the browser demo. Twilio, Telegram and a public tunnel together unlock real phone calls.

| Provider | Used for | Required for | Env vars |
|---|---|---|---|
| Anthropic | Excuse compiler, live caller turns | Everything | `ANTHROPIC_API_KEY`, `LLM_FAST_MODEL`, `LLM_STRONG_MODEL` |
| Twilio Programmable Voice + ConversationRelay | Outbound call, speech-to-text, text-to-speech | Real calls | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` |
| Telegram Bot API | Trigger channel, phone number verification | Real calls | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET` |
| cloudflared or ngrok | Public HTTPS + WSS URL for local development | Real calls (local) | `PUBLIC_BASE_URL` |
| Browser Web Speech API | Speech in the demo page | Demo | none |
| Redis (Docker) | Short-lived call scripts | Optional | `REDIS_URL` |

Facts below were checked against the official documentation in November 2026. Links are at the end of each section.

---

## Anthropic

**Used for.** Two calls per excuse flow:

1. The **excuse compiler** turns a short message into a `CallScript` with one structured-output request to `claude-sonnet-5-5`.
2. The **caller** speaks each turn of the call with a streaming request to `claude-haiku-5-5`. Latency matters here; quality matters in the compiler.

**Credentials.**

1. Sign in at https://platform.claude.com.
2. Add billing under **Settings > Billing** (the API has no free tier).
3. Go to **Settings > API keys**, click **Create key**, name it `imbusy`, copy it into `ANTHROPIC_API_KEY`.

**SDK.** `anthropic` (Python) 2.x, async client `anthropic.AsyncAnthropic`. The 2.x line sends API version `2026-10-21`, which rejects `temperature`, `top_p` and `top_k`; the code never sends them. Python 3.10+.

**Compiler request** (structured output, validated into a Pydantic model):

```python
response = await client.beta.messages.parse(
    model="claude-sonnet-5-5",
    max_tokens=4000,
    system=compiler_prompt,
    messages=[{"role": "user", "content": "tía Carmen, se ha dejado las llaves dentro, en 2 min"}],
    output_format=ExcuseDraft,
    output_config={"effort": "low"},
    betas=["server-side-fallback-2026-07-01"],
    fallbacks="default",
)
if response.stop_reason == "refusal":
    ...  # becomes CompileRefused(kind=HARMFUL)
draft = response.parsed_output
```

On Sonnet 5.5, thinking cannot be disabled (`{"type": "disabled"}` returns 400); `output_config.effort` is the lever. Forced `tool_choice` returns 400, which is why the compiler uses structured outputs, not a forced tool. `fallbacks="default"` reroutes a classifier refusal to a fallback model server-side.

**Caller request** (streaming text, thinking off for the shortest time to first token):

```python
async with client.messages.stream(
    model="claude-haiku-5-5",
    max_tokens=300,
    system=persona_prompt,
    messages=history,
    thinking={"type": "disabled"},
    output_config={"effort": "low"},
) as stream:
    async for text in stream.text_stream:
        await relay.send(TextToken(token=text))
    final = await stream.get_final_message()
```

Haiku 5.5 accepts `thinking: {"type": "disabled"}` at effort `low`, `medium` and `high`. Assistant prefill returns 400 on current models, so the persona is steered from the system prompt only. Read content blocks by `type`, never by position.

**Limits and costs.** List prices per million tokens (prompts up to 100K tokens): Haiku 5.5 $0.10 input / $0.50 output; Sonnet 5.5 $2 / $10. A five-turn call is about 5 Haiku requests of ~1.5K input and ~60 output tokens, well under a cent. One compile is ~2K in / ~400 out on Sonnet, about a cent. Rate limits depend on the account's usage tier (Console > Settings > Limits).

**Docs read.**
- https://platform.claude.com/docs/en/about-claude/models/overview (model ids, prices, default effort)
- https://pypi.org/project/anthropic/ (2.8.0, Python >= 3.10)
- Further reference: structured outputs (https://platform.claude.com/docs/en/build-with-claude/structured-outputs), streaming (https://platform.claude.com/docs/en/build-with-claude/streaming), thinking (https://platform.claude.com/docs/en/build-with-claude/thinking), API version 2026-10-21 migration (https://platform.claude.com/docs/en/api/migration-2026-10-21).

---

## Twilio: Programmable Voice and ConversationRelay

**Used for.** Placing the outbound call to the user and running the conversation. ConversationRelay does speech-to-text and text-to-speech on Twilio's side and talks to this server over a WebSocket in plain text: Twilio sends what the user said; the server streams back what the caller says.

### Read this first: trial accounts cannot run this

Twilio's current free trial blocks `<ConversationRelay>` (and `<Stream>`): on a trial account the verb is stripped and replaced with a `<Say>` saying it is not available. Trials are also limited to calls inside your sign-up country, to up to 5 verified numbers, to 10-minute calls and 75 voice minutes, use a shared Twilio-owned caller number, and expire after 30 days. To place real I'm Busy calls you must **upgrade the account** (add a payment method). Upgraded accounts get a ConversationRelay free allocation (listed as 30 units on the trial page). The browser demo exists precisely so nobody needs to do this to try the project.

### Credentials and one-time setup

1. Create an account at https://www.twilio.com/try-twilio and verify your email and phone.
2. **Upgrade**: click the **Trial** banner (top left) or **Upgrade your account**, then **Upgrade**, and add a payment method.
3. **Buy a number**: **Phone Numbers > Manage > Buy a number**. A US local number with Voice capability is the cheapest and needs no paperwork. Copy it in E.164 form into `TWILIO_FROM_NUMBER`.
4. **Accept the AI/ML addendum** (required before ConversationRelay works): **Communications > Voice > Settings > Privacy & Security**, accept the terms under "These features are subject to the predictive and generative AI/ML features addendum", click **Save**. In the legacy console: **Voice > Settings > General**, turn on **Predictive and Generative AI/ML Features Addendum**.
5. **Allow the destination country**: **Voice > Settings > Geo permissions**, select the country of your own mobile (e.g. Spain), enable **Low-Risk** numbers, **Save**. Leave high-risk ranges off.
6. **Account SID and Auth Token**: Console home, **Account Info** panel. Copy into `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN`. The auth token is also the key for signature validation, so the code uses it rather than an API key pair.

No TwiML App or number webhook is needed: the server creates calls with inline TwiML.

**Calling a Spanish mobile from a US number.** It works once Spain is enabled in Geo permissions; the caller ID shows the +1 number. Save that number in your contacts as "Tía Carmen" and the illusion holds. Buying a Spanish number is possible but requires identity documents, a Spanish tax ID (DNI/NIF/NIE) and a Spanish address, and from 17 October 2026 Spanish rules restrict which ranges may be used for outbound commercial calls (these personal calls are not commercial).

### Outbound call (REST, inline TwiML)

`POST https://api.twilio.com/2010-04-01/Accounts/{AccountSid}/Calls.json`, HTTP Basic auth. With the helper library (`twilio` 9.x):

```python
from twilio.rest import Client

client = Client(account_sid, auth_token)
call = client.calls.create(
    to="+34600111222",                    # always the requester's own verified number
    from_="+14155550100",
    twiml=twiml,                          # inline TwiML, max 4000 characters
    status_callback=f"{base}/twilio/status?call_id={call_id}",
    status_callback_event=["initiated", "ringing", "answered", "completed"],
    timeout=25,                           # seconds of ringing; default 60, max 600
)
call.sid, call.status                     # "CA...", "queued"
```

`Twiml` is ignored if `Url` is also given. Status values: `queued`, `ringing`, `in-progress`, `completed`, `busy`, `failed`, `no-answer`, `canceled`. The library is synchronous; the app calls it from a worker thread.

### TwiML

```xml
<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect action="https://PUBLIC/twilio/connect-action">
    <ConversationRelay url="wss://PUBLIC/twilio/relay"
                       welcomeGreeting="¿Mari? Oye, soy Carmen, ¿tienes un segundo?"
                       welcomeGreetingInterruptible="speech"
                       language="es-ES"
                       ttsProvider="Google"
                       voice="es-ES-Chirp3-HD-Aoede"
                       interruptible="speech"
                       dtmfDetection="false">
      <Parameter name="call_id" value="6f1c..."/>
    </ConversationRelay>
  </Connect>
</Response>
```

Attributes this project relies on (full list in the reference):

| Attribute | Value here | Notes |
|---|---|---|
| `url` | `wss://.../twilio/relay` | Required, must be `wss://` |
| `welcomeGreeting` | the script's `opening_line` | Spoken on pickup with no model latency |
| `welcomeGreetingInterruptible` | `speech` | `none`, `dtmf`, `speech`, `any` (default `any`) |
| `language` | `es-ES` or `en-US` | Sets both TTS and STT language (default `en-US`) |
| `ttsProvider` | `Google` | `Google`, `Amazon`, `ElevenLabs` (default `ElevenLabs`) |
| `voice` | catalog voice | Provider-specific; invalid pairs end the session with error 64101 |
| `transcriptionProvider` | default | `Deepgram` (default; `nova-3-general` for es-ES) or `Google` |
| `interruptible` | `speech` | Caller speech stops TTS; Twilio sends `interrupt` |
| `interruptSensitivity` | default `high` | `high`, `medium`, `low` |
| `<Parameter>` | `call_id` | Delivered in `setup.customParameters` |

Other attributes worth knowing: `speechTimeout`, `turnDetection`, `eotThreshold`, `partialPrompts`, `reportInputDuringAgentSpeech`, `preemptible`, `hints`, `events`, `profanityFilter` (default true), `applicationReconnect`, `debug`.

**Voices.** Catalog voices live in `src/imbusy/call/voices.py`. Twilio's TTS table lists for es-ES: Google Chirp3-HD (Aoede, Kore, Leda, Zephyr female; Charon, Fenrir, Orus, Puck male), Neural2 (`es-ES-Neural2-G` male, `-H` female), WaveNet and Standard; Amazon Polly `Lucia-Neural`/`Lucia-Generative` (female), `Sergio-Neural`/`Sergio-Generative` (male). ElevenLabs' es-ES default voice id is `6xftrpatV0jGmFHxDjUv` (format `VOICEID-model-speed_stability_similarity`). Only stock voices are used; no cloning.

### WebSocket messages

Twilio to server:

| `type` | Fields used | When |
|---|---|---|
| `setup` | `sessionId`, `callSid`, `from`, `to`, `direction`, `callStatus`, `customParameters` | Once, right after connecting |
| `prompt` | `voicePrompt`, `lang`, `last` | User finished speaking (`last: true` unless `partialPrompts`) |
| `interrupt` | `utteranceUntilInterrupt`, `durationUntilInterruptMs` | User spoke over the caller |
| `dtmf` | `digit` | Key press, only with `dtmfDetection="true"` |
| `info` | `name`, `value` | Subscribed events, e.g. `configUpdated` |
| `error` | `description` | Errors such as 64107 (invalid message); the session continues |

Server to Twilio:

```json
{"type": "text", "token": "Oye, ", "last": false}
{"type": "text", "token": "que me he dejado las llaves dentro.", "last": true}
{"type": "end", "handoffData": "{\"reason\":\"script-complete\"}"}
```

`text` also accepts `interruptible`, `preemptible` and `lang`; tokens must keep their spaces. Other server messages: `play`, `sendDigits`, `language`, `config`, `hints`. Ten consecutive unrecognised messages close the socket with code 1007.

**Ending the call from the server.** Send `{"type": "end"}`. ConversationRelay hands control back to Twilio, which POSTs to the `<Connect action>` URL with `SessionStatus` (`ended` when the app ended it, `completed` when the caller hung up, `failed` on error), `SessionId`, `SessionDuration`, `HandoffData`, and `ErrorCode`/`ErrorMessage` on failure. That endpoint returns `<Response><Hangup/></Response>`. If the socket drops unexpectedly and `applicationReconnect` is not set, the call ends as `failed`.

### Signature validation

Every request from Twilio carries `X-Twilio-Signature`: HMAC-SHA1 of the full URL (with query string) followed by the POST parameters sorted by name, keyed with the auth token, base64-encoded. Use `twilio.request_validator.RequestValidator(auth_token).validate(url, params, signature)`.

- **HTTP callbacks** (`/twilio/status`, `/twilio/connect-action`): pass every form field, and the URL exactly as Twilio called it.
- **WebSocket handshake**: ConversationRelay sends the header on the upgrade request; read it as lowercase `x-twilio-signature`, validate against the `wss://` URL exactly as written in the TwiML, with no parameters. Reject before `accept()`.
- **Behind a tunnel** the app sees `http://localhost:8000/...`. Rebuild the URL from `PUBLIC_BASE_URL`; never validate against `request.url`.

### Limits and costs (list prices, check the pricing page)

- ConversationRelay: $0.07 per minute, on top of voice minutes.
- Outbound call to a Spanish mobile: $0.18 per minute as listed on the Spain voice pricing page.
- US local number: about $1.15 per month.
- A 90-second call to a Spanish mobile is therefore roughly $0.40 in Twilio charges.

**Docs read.**
- https://www.twilio.com/docs/voice/twiml/connect/conversationrelay
- https://www.twilio.com/docs/voice/conversationrelay/websocket-messages
- https://www.twilio.com/docs/voice/conversationrelay/onboarding
- https://www.twilio.com/docs/voice/conversationrelay/voice-configuration
- https://www.twilio.com/docs/voice/twiml/say/text-speech
- https://www.twilio.com/docs/voice/api/call-resource
- https://www.twilio.com/docs/usage/webhooks/webhooks-security
- https://www.twilio.com/docs/usage/trials and https://www.twilio.com/docs/usage/trials/try-out-voice
- https://www.twilio.com/docs/voice/tutorials/voice-dialing-geographic-permissions
- https://www.twilio.com/en-us/voice/pricing/es
- https://www.twilio.com/en-us/guidelines/es/regulatory
- https://pypi.org/project/twilio/ (9.13.0)

---

## Telegram Bot API

**Used for.** The user sends the excuse to a bot; the bot also collects the user's own phone number, which is the only number the system will ever call. For the safe word, the same bot stores the phrase, invites one trusted contact through a link, receives the user's shared location, and sends the alert.

**Credentials.**

1. In Telegram, open a chat with **@BotFather**, send `/newbot`, pick a display name and a username ending in `bot`.
2. Copy the token (`123456789:AA...`) into `TELEGRAM_BOT_TOKEN`.
3. Invent a webhook secret (1-256 characters from `A-Z a-z 0-9 _ -`) and put it in `TELEGRAM_WEBHOOK_SECRET`:
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
4. With the server reachable at `PUBLIC_BASE_URL`, run `uv run poe set-webhook`.

**Endpoints.** All methods are `POST https://api.telegram.org/bot<token>/<method>` with a JSON body. Responses are `{"ok": true, "result": ...}` or `{"ok": false, "error_code": 400, "description": "..."}`. Bot API version at time of writing: 10.4.

`setWebhook`:

```json
{
  "url": "https://PUBLIC/telegram/webhook",
  "secret_token": "your-secret",
  "allowed_updates": ["message", "edited_message"],
  "drop_pending_updates": true
}
```

`edited_message` is needed because a live location is delivered as edits of the original location message (each edit carries `edit_date` and the new coordinates). Every other edit is ignored. Telegram then sends every update with header `X-Telegram-Bot-Api-Secret-Token: your-secret`; anything else is rejected. Webhooks must be HTTPS on port 443, 80, 88 or 8443 (tunnels use 443).

**Incoming update** (the fields the bot reads):

```json
{
  "update_id": 1000,
  "message": {
    "message_id": 7,
    "from": {"id": 42, "is_bot": false, "first_name": "Mari", "language_code": "es"},
    "chat": {"id": 42, "type": "private"},
    "date": 1763050000,
    "text": "tía Carmen, se ha dejado las llaves dentro, en 2 min"
  }
}
```

**Phone verification by contact share.** `/start` replies with a one-button keyboard:

```json
{
  "chat_id": 42,
  "text": "Share your number so I know where to call you. I will only ever call this number.",
  "reply_markup": {
    "keyboard": [[{"text": "Share my number", "request_contact": true}]],
    "resize_keyboard": true,
    "one_time_keyboard": true
  }
}
```

`request_contact` works in private chats only and sends the user's own phone number as a `Contact` (`phone_number`, `first_name`, optional `user_id`). The bot accepts it only when `contact.user_id == message.from.id`; a contact forwarded from the address book has no `user_id` or a different one, and is refused. `phone_number` may arrive without a leading `+`, so it is normalised to E.164. Telegram verified that number by SMS when the account was created, so no separate OTP is sent.

`sendMessage`: `chat_id`, `text` (1-4096 characters), optional `reply_markup` (`ReplyKeyboardMarkup` or `{"remove_keyboard": true}`). No `parse_mode`, so user-supplied names never need escaping.

**Safe word: the calls it adds.** Setup is a few taps, and each step maps to one Bot API feature.

- `/safeword pink flamingo` stores the phrase in normalised form (two to four words, at least eight letters). The bot never repeats it back.
- `/trusted` creates a random token (32 URL-safe characters) and replies with a deep link, `https://t.me/<bot_username>?start=trust_<token>`. Deep-link parameters may use `A-Z a-z 0-9 _ -` and be up to 64 characters; when the person opens the link and presses Start, the bot receives the text `/start trust_<token>` from them. The bot's username comes from `getMe` (`result.username`), called once and cached.
- The invited person gets an explanation and answers `/accept` or `/decline`. This step is not optional: a bot cannot start a conversation with someone who has never started it (`sendMessage` fails with 403 "bot can't initiate conversation with a user"), so the contact's own tap is both their consent and what lets the bot message them later. `/leave` undoes it.
- `/location` sends a keyboard with `{"text": "Share my location", "request_location": true}` (private chats only). The message comes back with `location` (`latitude`, `longitude`, optional `horizontal_accuracy` in metres). A live location shared from the attachment menu also carries `live_period`, and its updates arrive as `edited_message`. The bot keeps only the latest point, for `LOCATION_TTL_SECONDS` (default one hour), in Redis with an expiry or in process memory; `/forgetlocation` deletes it.
- When the safe word is heard, the alert is a `sendMessage` to the contact's chat followed by `sendLocation`:

```json
{"chat_id": 5151, "latitude": 40.416775, "longitude": -3.70379, "horizontal_accuracy": 18.4}
```

`sendLocation` accepts `horizontal_accuracy` from 0 to 1500 metres; the client caps it there. What I promise is honest to what Telegram gives: the last location the user chose to share, how old it is, and its stated accuracy. It is not tracking, and it is not a position at the moment of the alert unless a live location was running.

**Limits.** Free. Broadcast limit about 30 messages per second, irrelevant at this scale. If the webhook does not answer quickly Telegram retries, so the server acknowledges first and works in the background, and deduplicates by `update_id`.

**Docs read.** https://core.telegram.org/bots/api (setWebhook, Update, Message, Contact, Location, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, sendMessage, sendLocation, getMe) and https://core.telegram.org/bots/features#deep-linking. The rule that a bot cannot message a user who has not started it is not stated on those pages; it is the documented behaviour of the 403 error and is consistent across client libraries, so the design does not depend on getting around it.

---

## Public tunnel for local development

Twilio and Telegram must reach the laptop over HTTPS, and ConversationRelay over WSS. Both tools below provide both on one hostname.

| | cloudflared quick tunnel | ngrok (free) |
|---|---|---|
| Account | None | Free account + authtoken |
| Command | `cloudflared tunnel --url http://localhost:8000` | `ngrok http 8000` |
| URL | Random `*.trycloudflare.com`, new on every start | One fixed dev domain `*.ngrok-free.app` |
| Limits | 200 in-flight requests, no SSE, no uptime guarantee | 1 GB/month out, 20,000 HTTP requests/month, 4,000/min |
| Browser pages | Served as is | Interstitial warning for browsers (not for Twilio/Telegram); skip with header `ngrok-skip-browser-warning` |
| WebSockets | Work in practice; not stated on the quick-tunnel page | Supported (not restated on the limits page) |

Recommendation: **ngrok** if you will test more than once, because the URL is stable and you set the Telegram webhook only once. **cloudflared** for a zero-signup first try; after each restart update `PUBLIC_BASE_URL`, restart the app and run `uv run poe set-webhook` again. A quick tunnel refuses to start if a `config.yaml` exists in `~/.cloudflared`. `docker compose --profile tunnel up` starts cloudflared next to the app.

Install on Windows: `winget install --id Cloudflare.cloudflared` or `winget install ngrok.ngrok` (package ids not verified against the docs; the download pages are authoritative).

**Docs read.**
- https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/
- https://ngrok.com/docs/pricing-limits/free-plan-limits/

---

## Browser demo: Web Speech API

**Used for.** The demo page replaces Twilio: it listens with `SpeechRecognition`, speaks with `speechSynthesis`, and talks to the same `CallSession` over `/demo/ws` using ConversationRelay-shaped frames (`prompt`, `interrupt` from the page; `text`, `end` from the server).

**Support (November 2026).**

- `speechSynthesis` is Baseline (all current browsers). Voices come from the operating system and vary; `getVoices()` may be empty until `voiceschanged` fires.
- `SpeechRecognition` is **not** Baseline. Chrome (desktop and Android) supports it, exposed as `webkitSpeechRecognition`, and sends audio to a server, so it does not work offline. Safari (macOS 14.1+, iOS 14.5+) has partial support. Firefox ships it disabled. Edge is listed as unsupported by caniuse. The microphone needs a secure context (HTTPS or `localhost`).

**Safe word in the demo.** The Safe word panel can include the browser's position through `navigator.geolocation.getCurrentPosition`, which works only on HTTPS or `localhost` and only after the visitor allows it. The position is sent with that one excuse, kept in server memory under the demo call token, and shown back in the alert preview; the demo never sends it anywhere else.

**Stated limitation.** The full voice demo needs Chrome; everywhere else the page falls back to typing your side of the call while the caller still speaks. The browser voices are not the phone voices, and the browser's recognition is not Twilio's, so demo latency numbers are labelled separately from phone numbers.

**Docs read.**
- https://developer.mozilla.org/en-US/docs/Web/API/SpeechRecognition
- https://developer.mozilla.org/en-US/docs/Web/API/SpeechSynthesis
- https://caniuse.com/speech-recognition

---

## Redis

Optional. `redis:7.4-alpine` in `docker-compose.yml`, used as a TTL store for compiled scripts (`SET imbusy:script:<call_id> <json> EX <delay+600>`). With `REDIS_URL` empty the app uses an in-process store, which is fine for the demo and a single process. Python client: `redis` 8.x (`redis.asyncio`).
