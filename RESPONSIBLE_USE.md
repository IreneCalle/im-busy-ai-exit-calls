# Responsible use

I'm Busy calls you, on your own phone, with a made-up caller who backs up a harmless excuse so you can leave somewhere: a date that is going badly, a meeting that will not end, a situation where you would rather not stay. It is an AI system. The caller's words come from a language model and the voice is a stock synthetic voice. It was built with safety in mind, and it is not an emergency service.

## What it is for

You give it two things: a name and an excuse. The name is a first name or a relationship ("tía Carmen", "my manager Dana"). The excuse is ordinary and harmless ("she has locked herself out", "the client moved the call"). The call goes to you, and only you, and lasts a few turns.

## What it refuses

These are checked in code before any model is asked (`src/imbusy/safety/acceptable_use.py`), stated again in the compiler's prompt, and checked a second time on what the model returns:

- Playing an identifiable real person: a full name, a public figure, or a request to sound like someone real ("make it sound like my actual boss", "use my mum's voice").
- Playing an authority or an organisation: the police, emergency services, a court, a hospital, a bank, an insurer, a government office, a company or its support line.
- Anything aimed at someone else: calling another number, deceiving a third party, or anything involving money, transfers, codes, passwords or personal data.
- Harassment: repeated calls, threats, scaring or humiliating anyone.

The checks are deliberately simple and err on the side of refusing. If a harmless excuse is refused, rephrase it with a first name and the situation, and consider opening an issue so the rule can be improved.

## Design limits, and why

- It only ever calls the verified number of the person who asked. The number comes from Telegram's own contact-sharing button and must belong to the account that shared it. There is no field anywhere for a destination number, so it cannot be pointed at someone else.
- Voices come from a fixed catalogue of stock voices. There is no voice cloning and nowhere to upload a voice sample.
- Each person gets a small number of calls per day (five by default), and the web demo is limited per visitor.
- No call transcripts are stored. A call record keeps the caller's name, the outcome, the number of turns and the response time, and `/forget` deletes it along with your number.
- The optional safe word sends one message to one person you chose, who agreed to receive it. It is not an emergency service and says so every time.

## What you commit to

By using it you agree to use it only for yourself, to give it a first name and an excuse rather than anyone's real identity, and not to use it to deceive, pressure or harm anyone. If you are in danger, call your local emergency number (112 in Europe, 911 in the US) rather than relying on this tool.

## Reporting misuse

If you see this project or a deployment of it being used to impersonate someone, to deceive or to harass, or you find a way around the checks above, please report it. Open an issue on the repository, or, if you would rather not post publicly, use GitHub's private vulnerability reporting on the repository. Please include what happened and, if you can, the message that got through. I read every report.
