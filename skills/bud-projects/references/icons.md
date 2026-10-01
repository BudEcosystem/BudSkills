# Project icons

Every project stores a single `icon` string. The console renders it on the
project card. A project created without an `icon` saves `NULL` and shows a blank
tile - the web console never omits it (it defaults to the globe `🌐`), so a skill
creating a project must not omit it either.

This file is the full reference; the short version lives in `SKILL.md` under
"Choosing a project icon".

## What the backend accepts

The `icon` value is validated server-side. It passes for exactly two forms:

1. **An emoji** that is on the platform's allow-list (a fixed `EMOJIS`
   constant - ~1800 entries covering the common emoji set).
2. **A relative path to a file that already exists under the server's static
   dir**, e.g. `icons/providers/openai.png`. These are "asset keys" - the
   console serves them from `<api>/static/<key>`.

Everything else fails validation:

- full URLs (`https://img.logo.dev/...`)
- `data:` URIs
- root-relative public paths (`/images/drawer/zephyr.png`, `/agents/icons/send.svg`)

These last three may *render* in some UIs but are not valid project icons.

### The create-vs-edit gotcha

`POST /projects/` currently does **not** run `validate_icon` (the validator is
commented out server-side), so a create will accept any string - including an
invalid URL. But `PATCH /projects/{id}` **does** validate. So an invalid icon
slips in at create time and then **cannot be edited** until it is replaced with a
valid value (the PATCH 422s). Always choose a valid value (emoji or existing
asset key) up front.

## Selection order

Resolve the icon in this order and stop at the first that yields a value:

### 1. Reuse the related entity's icon - only if safe

If the project is being created for/around a specific deployed **agent**,
**model**, or **endpoint**, read that entity's icon and reuse it **only if** it
is an emoji or an existing static asset key.

Where the source icon lives:

| Source | Field | Typical form |
|---|---|---|
| Agent | `a2a_card.icon_url` (also read-only `model_icon`) | often a URL / `/public` path - usually **unsafe** |
| Model | `icon` (fallback `provider.icon`) | often an asset key like `icons/providers/openai.png` - **safe** |
| Endpoint / deployment | no own icon; use `model.icon` | as per Model |

Safety check before copying a source value `v`:

```
safe(v)  ==  is_emoji(v)                      # 1-3 chars, not a path/url
          OR ( "/" in v
               and not v.startswith(("http://","https://","data:","/")) )
             # i.e. a bare "dir/file.ext" asset key, not a URL or /public path
```

If `safe(v)` is false (a URL, `data:`, or `/public` path), **do not copy it** -
fall through to step 2. Copying it would both fail a later edit and diverge from
the emoji-first look of project tiles.

To fetch an agent's card icon:

```bash
bud api GET /prompts/<agent-id> | jq -r '.prompt.a2a_card.icon_url // empty'
```

### 2. Infer a fitting emoji from the project's purpose

Pick from the project `name`, `tags`, and `description`, choosing from the safe
set below. Rough map:

| Theme | Emoji |
|---|---|
| HR / people / staffing | 💼 |
| Banking / finance | 🏦 |
| Health / medical | 🏥 |
| Research / science | 🔬 |
| Data / analytics | 📊 |
| Agent / bot / automation | 🤖 |
| Docs / knowledge | 📚 |
| Security / compliance | 🛡️ |
| Legal | ⚖️ |
| Ideas / innovation | 💡 |
| General / nothing fits | 🌐 |

### 3. Ask the user, when a human is in the loop

If the runtime exposes an interactive question tool, ask. The bda-desktop agent
exposes `ask_user_questions` - the conversation pauses until the user answers,
and each question supports `options`, a `recommended_index`, and
`allow_freeform`.

Set the step 1/2 result as the recommended option, e.g.:

```json
{
  "questions": [
    {
      "prompt": "Pick an icon for the \"HR Assistant\" project",
      "options": ["💼", "🧑‍💼", "🤖", "📚", "🌐"],
      "recommended_index": 0,
      "allow_freeform": true
    }
  ]
}
```

The tool result comes back as `{"answers": [...]}`. Use the chosen value; if the
user cancels, fall back to the recommended one. Only ask once per project - do
not block a batch create on a question per project; infer instead.

### 4. Default

If nothing above produced a value, use `🌐` - the console's own default.

## Safe emoji set

All verified present on the platform allow-list, so they survive edit-validation:

```
🌐 🤖 🚀 💡 📊 🔬 🧠 💬 🏦 🏥 ⚙️ 📚 🛡️ ⚖️ 📝 💼 🔭 📦 🔑 ✨
```

Other common emojis are generally on the list too, but when you need a guarantee,
pick from this set.

## No custom image upload

There is no endpoint to upload a project icon image, and `bud api` sends JSON
only (no multipart). So a project icon is always an emoji or an existing static
asset key - not an arbitrary uploaded file. If a user insists on a custom image,
explain this limitation rather than attempting an upload.

## Changing an icon later

```bash
bud api PATCH /projects/$BUD_PROJECT_ID -d '{"icon":"🏦"}'
```

Remember this path **does** validate - send a safe value (emoji or existing
asset key).
