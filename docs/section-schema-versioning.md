# Section schema versioning

Custom sections are resolved on the server (`/sections/resolved`) and rendered by
the app. As new section capabilities ship, older app builds must not try to render
things they don't understand. A single dotted **section-rendering contract version**
coordinates this between backend and app.

- Backend: `SECTION_SCHEMA_VERSION` in `app/config.py`.
- App: `ClientSchema.version` (kept in lockstep), which the app announces on every
  sections request via the `X-Poptonium-Client-Schema` header. Absent header (older
  builds, or the admin dashboard) is treated as the `1.0.0` baseline.

Every resolved section carries a `min_app_version`. The app renders a section only
when `ClientSchema.version >= min_app_version`; otherwise it drops it.

## Where a section's `min_app_version` comes from

`section_min_version(type, style, config)` returns the **highest** of three floors:

1. **Type floor** — `SECTION_TYPE_MIN_VERSION` (e.g. `filter`, `history`).
2. **Style floor** — `SECTION_STYLE_MIN_VERSION` (e.g. `row`, `hero`, `bento`).
3. **Feature floors** — `SECTION_FEATURE_MIN_VERSION`: opt-in **config flags** that
   need a newer client than the section's type/style alone. Keyed by the cfg flag
   that enables the feature, e.g. `{"episode_items": "1.1.0"}`.

Feature floors are the important addition: a plain `filter`/`row` section renders on
`1.0.0`, but the *same* section with `episode_items: true` in its config requires
`1.1.0`, because it emits individual episode cards an older app can't draw.

## Two ways a section can be gated

### 1. Graceful degradation (preferred, when the feature has a fallback)

If a feature degrades to something older clients already render, don't hide the
section — serve the supported form. `degrade_config(config, client_schema)` returns a
copy of the config with any feature the client is too old for switched **off**, and
the section is both resolved and version-stamped from that degraded config.

Example — `episode_items` degrades to whole shows:

| Client | Served items | `min_app_version` |
|--------|--------------|-------------------|
| `1.0.0` (or no header) | whole **show** cards | `1.0.0` — rendered |
| `1.1.0` | individual **episode** cards | `1.1.0` — rendered |

Because the stamped `min_app_version` is computed from the *degraded* config, it never
exceeds what was actually sent, so the old client still renders the section. This
degradation is applied in `resolve_section` (items) **and** in the `/sections` shells
listing (`_section_to_dict`), so an old app's skeleton and resolved section agree.

### 2. Hard skip (when there is no fallback)

A wholly new `type`/`style` with nothing an old app can draw just gets the higher
floor. Old apps evaluate `min_app_version > ClientSchema.version` and drop the section
(and its skeleton) rather than mis-render it. Nothing else is needed.

## Adding a new gated feature

1. **Backend**: add the cfg flag → required version to `SECTION_FEATURE_MIN_VERSION`,
   bump `SECTION_SCHEMA_VERSION`, and implement the resolver behavior. If the feature
   has an older-client fallback, make sure the resolver reads the (possibly degraded)
   flag so degradation actually changes what's resolved.
2. **App**: implement rendering for the new representation, send/keep the
   `X-Poptonium-Client-Schema` header, and bump `ClientSchema.version` to match — only
   once the app can actually render it.

Because the server can ship first (old apps degrade or skip; nothing mis-renders), the
backend change is safe to deploy independently of the app release.
