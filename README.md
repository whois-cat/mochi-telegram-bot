# Mochi Telegram Bot

Telegram bot for adding English vocabulary to Mochi.

## Roles

Mochi is the source of truth for cards and SRS review.

The Telegram bot is an operational layer for:

- adding cards to Mochi (manually or generated with Gemini)
- editing/deleting cards and syncing those changes to Mochi

The bot does not run its own practice or reviews, and does not send reminders.

## Commands

- `/ai word_or_phrase` - generate a card **draft** with Gemini; buttons `To Mochi` / `Inbox` decide where it goes. Without a button press the word is not sent anywhere.
- `/mochi word_or_phrase` - generate a card and send it straight to Mochi
- `/inbox word_or_phrase` - generate a card and park it in the inbox (not in Mochi)
- `/inbox` - list inbox words, most-seen first
- `/add word | translation | usage | example` - add a card draft manually
- `/delete word` - delete the local record and, if it is in Mochi, the Mochi card
- `/cancel` - cancel the current edit session

`/ai`, `/mochi` and `/inbox` accept several words: one per line or separated by `;`, up to 10. Words that were just sent to Mochi or Inbox are confirmed in one message (`✓ word → Mochi`); drafts and already saved words get their own card with buttons. Words that are already saved do not spend a Gemini call.

Practice commands (`/today`, `/stats`) and older review modes (`/practice`, `/weak`) were removed; sending them shows the help.

## Drafts, Inbox and Mochi

Each word in DynamoDB has a `status`:

- `DRAFT` - generated, not sent anywhere yet
- `INBOX` - parked; not in Mochi
- `RESERVED` -> `CREATED` - being sent to / now in Mochi
- `FAILED` - the Mochi request failed; `To Mochi` retries it

Gemini also estimates `frequency` (`common` / `moderate` / `rare`) and a CEFR `level`. Both are shown in Telegram and added as Mochi tags (`freq-common`, `b2`).

Sighting counter: adding a word again while it is a draft or in the inbox increments `seen_count` instead of replying "exists". From 3 sightings the reply suggests sending the word to Mochi.

Getting words out of the inbox:

- `To Mochi` on a card or next to a word in `/inbox`
- `Delete` next to a word in `/inbox`
- `Move seen 2+ times to Mochi` under `/inbox` - sends up to 10 such words per press
- `/mochi word` for a word that is already in the inbox moves it to Mochi

Regenerate and Edit change Mochi only for words that are already there; drafts and inbox words stay local. `POST /add-card` still sends straight to Mochi.

## Editing

Every card reply shows `Regenerate`, `Edit` and `Delete` (plus `To Mochi` / `Inbox` for drafts and inbox words).

`Edit` starts a short session where the user sends:

```text
word | translation | usage | example
```

If the word is in Mochi, edits are synced to Mochi.

## Telegram Command Menu

After deploying, refresh Telegram's slash-command menu with the protected API route:

```text
POST /sync-telegram-commands
x-bot-secret: <APP_SECRET>
```

This overwrites Telegram's bot command list with the current commands and removes old entries such as `/today`, `/stats`, `/practice` and `/weak`.

## Manual Test Checklist

1. `/ai reliable` - a draft with frequency/level and `To Mochi` / `Inbox` buttons; nothing appears in Mochi.
2. `/ai reliable` again - "seen 2×".
3. Tap `Inbox`, then `/inbox` - the word is listed with `seen 2×` and the promote button.
4. Tap `Move seen 2+ times to Mochi` - the card appears in Mochi with `freq-*` and level tags.
5. `/mochi` with several words on separate lines - one reply per word, all in Mochi.
6. Tap `Edit` on an inbox word and send `word | translation | usage | example` - Mochi is not touched.
7. Send `/today` - the bot replies with help.
