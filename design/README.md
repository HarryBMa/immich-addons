# Design reference

Source of truth: Claude Design project **"Immich addon store design"** —
<https://claude.ai/design/p/5c845870-7c8e-4d34-b983-0e6f266bb7e3>
(`Immich Addon Store.dc.html`, rendered by the `support.js` dc-runtime).

Four screens, each 1280×820, drawn inside the Immich shell (topbar + sidebar, "Addons" sitting
under the `UTILITIES` group):

| # | Screen | What it establishes |
| --- | --- | --- |
| 01 | Store / Browse | 3-column addon card grid, category filter pills, `Installed · N` + `Install from URL` actions, `VERIFIED` badge, per-card capability line (`container · read-only · 18.4k`) with risky capabilities in the warn colour. |
| 02 | Addon detail | Breadcrumb, header w/ maintainer + version + updated, **Requested permissions** table incl. an explicit *Not requested* row, screenshot slot, right rail: install button, type/image/licence/server-min/installs, audit card. |
| 03 | Install / permission grant | Modal over a blurred catalog. "This addon will be able to" list, a plain-language *cannot* sentence, a "Before it can run" preflight checklist (✓ / !), auto-update checkbox, `Cancel` / `Grant & install`. |
| 04 | Installed & updates | Browse / Installed / Activity log tabs, update banner naming the *new* permission, one row per addon with status dot (Running / Update pending / Stopped) and Settings·Logs·⋯, footer tiles for addon RAM/CPU and monthly egress. |

## How this maps onto the build

The plan builds the hub as a standalone FastAPI + Jinja2/htmx service on `:8484` (PLAN.md §5), and
only Phase 8 puts it inside Immich's UI via an iframe. So the hub's own templates should adopt these
tokens and this layout verbatim — then the `?embedded=1` view reads as native Immich rather than as
a foreign app in a frame.

Screen → route:

- 01 Browse → `/` (catalog)
- 02 Detail → `/addons/{id}` (add the permission table above the generated config form)
- 03 Install grant → not v1: addons ship inside the hub image, there is no install step. The
  permission table and its *cannot* language are still worth keeping on the detail page — they are
  the honest place to state what each addon touches.
- 04 Installed → `/jobs` + the enabled toggles on the catalog

## Tokens

Light (default):

```
--canvas #f1f2f5   --surface #ffffff  --card #f6f7f9   --line #e4e6eb  --sunk #f0f1f4
--text   #1b1d22   --dim     #5e636c  --faint #8c9099
--accent #4250af   --accentFg #ffffff --navBg #e9ecfb  --navFg #3a4699
--warn   #9a5a0b   --ok      #2e7a4d  --imgA  #e3e5ea  --imgB #f1f2f5
--scrim  rgba(24,26,32,.45)           --dcRadius 14px
```

Dark (overrides only):

```
--canvas #111111   --surface #000000  --card #0d0d0d   --line #232323  --sunk #161616
--text   #fafafa   --dim     #a8a8a8  --faint #7a7a7a
--navBg  #182135   --navFg   #adcbfa  --accentFg #0d1526
--warn   #e8b17a   --ok      #7fbf8f  --imgA  #2a2a2a  --imgB #1e1e1e
--scrim  rgba(0,0,0,.62)
```

Type: **Overpass** for UI, **Overpass Mono** for identifiers, capability lines, counts and
section eyebrows (11–12 px, `letter-spacing:.14em`, uppercase). Cards `border-radius:12px` on a
1 px `--line` border; pills and buttons are fully rounded (`height/2`).

Load the fonts locally in the hub image rather than from Google Fonts — the hub must work on a LAN
with no internet, and Phase 8 serves it inside a CSP-restricted iframe.
