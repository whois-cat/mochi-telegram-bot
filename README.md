# Mochi Telegram Bot

Telegram bot for adding English vocabulary to Mochi and practicing active usage.

## Roles

Mochi is the source of truth for cards and classic SRS review.

The Telegram bot is an operational layer for:

- adding cards to Mochi
- editing/deleting cards and syncing those changes to Mochi
- generating active practice on request
- tracking active usage statistics for words and phrases

The bot does not replace Mochi's card review flow and does not send automatic reminders or scheduled exercises.

## Commands

- `/add word | translation | usage | example` - add a card manually
- `/ai word_or_phrase` - generate a card with Gemini and add it to Mochi
- `/delete word` - delete the local card record and the linked Mochi card
- `/today` - start active practice with 30 tasks
- `/stats` - show active practice stats
- `/cancel` - cancel the current edit or practice session

Removed Telegram review modes:

- `EN -> RU`
- `RU -> EN`
- `/weak`
- `/practice`
- due/due-today review flows

If `/weak` or `/practice` is sent, the bot treats it as an unknown command and shows the current help.

## Active Practice

`/today` creates a 30-task training session:

- 10 fresh AI-generated fill-in-the-blank English sentences
- 10 Russian-to-English sentence translations
- 10 own-sentence tasks

Fill-in-the-blank sentences are generated for each session. Stored card examples are only card content; they are not reused as practice sentences. When natural, the generated sentence also includes one or two additional vocabulary items from the current practice word bank as visible context.

The session is stored with:

- `training_id`
- `tasks`
- `current_task_index`
- `user_answers`
- `evaluation_results`
- `created_at`
- `completed_at`

The bot asks one task at a time. At the end it shows a summary with correct, minor issue, and wrong counts, plus words worth practicing again.

Each active task shows helper buttons:

- `Hint` - gives a local hint and keeps the same task active. It does not update word stats.
- `IDK` - records the task as `idk`, shows the answer/model answer, moves to the next task, and makes the word appear sooner in future practice.

Text equivalents also work: `hint`, `подсказка`, `дай подсказку`, `idk`, `i don't know`, `не знаю`, `не помню`, `забыла`.

Before using Gemini for evaluation, the bot runs local normalization and accepted-answer checks. It treats contractions/full forms, case-only differences, and simple punctuation differences as equivalent, for example `I am in the middle of` and `I'm in the middle of`.

## Practice Scheduling

Telegram keeps lightweight practice scheduling only for active exercises. It does not manage Mochi's card SRS.

Per-word practice fields include:

- `correct_count`
- `wrong_count`
- `idk_count`
- `minor_issue_count`
- `last_practiced_at`
- `last_correct_at`
- `last_wrong_at`
- `last_idk_at`
- `practice_interval_days`
- `next_practice_at`
- `practice_score`

Words with errors, minor issues, low correctness, old practice dates, or elapsed `next_practice_at` are more likely to appear in future `/today` sessions.

## Session Storage

Active practice sessions use compact DynamoDB storage:

- one small metadata item per active user session
- one separate item per task
- TTL on session/task items
- short stored feedback capped at 500 characters

Raw Gemini prompts/responses and long debug traces are not stored in DynamoDB. They belong in CloudWatch logs if needed.

## Editing

After adding or regenerating a card, Telegram shows:

- `Regenerate`
- `Edit`
- `Delete`

`Edit` starts a short session where the user sends:

```text
word | translation | usage | example
```

An optional fifth field can be provided for a cloze sentence:

```text
word | translation | usage | example | cloze sentence
```

If the card already exists in Mochi, edits are synced to Mochi.

## Migration

Existing DynamoDB items can be migrated with the protected API route:

```text
POST /migrate-practice-stats
x-bot-secret: <APP_SECRET>
```

The migration is idempotent. It preserves useful `correct_count` and `wrong_count` values, initializes new active-practice fields, and removes legacy Telegram card-SRS fields such as `review_count`, `streak`, `interval_days`, and `due_at`.

## Telegram Command Menu

After deploying, refresh Telegram's slash-command menu with the protected API route:

```text
POST /sync-telegram-commands
x-bot-secret: <APP_SECRET>
```

This overwrites Telegram's bot command list with the current commands and removes old entries such as `/practice` and `/weak`.

## Manual Test Checklist

1. Add a word through the bot and confirm `Regenerate`, `Edit`, and `Delete` still appear.
2. Start `/today`.
3. Tap `Hint`; the same task should remain active and stats should not update.
4. Tap `IDK`; the bot should show the answer and move to the next task.
5. Answer with a full form where the expected answer uses a contraction.
6. Try an own sentence with an awkward collocation; feedback should include `Better with "<target word>"`.
7. Finish a full session and check the summary still lists all words that need more practice.
