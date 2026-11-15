# B6 design tokens: v0.4 contract

`tokens.json` is the source for the generator. Version `0.4` fixes the top-level shape below; adding or renaming a top-level key requires a schema version bump. Reject unknown top-level keys and unsupported versions. Descriptive strings are metadata, never tokens to emit.

## Top-level keys

| Key | Type | Meaning |
| --- | --- | --- |
| `$schemaNote`, `version` | string | Schema description and exact version (`0.4`). |
| `color` | object | Named colors and aliases, including category wash aliases. |
| `categoryColor` | object | Opaque hex values by category identifier. |
| `typography` | object | `families` maps family identifiers to font names; `bodyFontNote` is metadata; `roles` maps role names to styles. |
| `space`, `radius` | object | Named nonnegative logical dimensions. |
| `size` | object | Named nonnegative dimensions, optionally grouped in nested objects. |
| `notes` | array of strings | Usage constraints and cautions. |
| `categoryNote`, `contrastNote` | string | Descriptions of usage and contrast semantics. |
| `contrast` | array | Explicit combinations to validate. |

Typography roles have `family` (a key in `typography.families`), numeric `size`, `weight` and `lineHeight`, and optional `features` (array of strings). Dimensions are logical dp/px; typography sizes and line heights are logical sp on Android, logical px on desktop. These values are not a physical-pixel contract. Preserve `tnum` as a font feature; validate that fonts are actually bundled and available.

## Color values and references

A value in `color` is one of:

- `"#RRGGBB"`, an opaque sRGB color.
- `{ "hex": "#RRGGBB", "css": "goldenrod" }`, an opaque named color; `hex` is the emitted value and `css` documents its intent. Validate that the supported CSS name resolves to that exact hex.
- `{ "ref": "text", "alpha": 0.60 }`, an alias with alpha from 0 to 1 inclusive. An unqualified reference names an entry in `color`; `color.name` explicitly names that same namespace; `categoryColor.name` names an entry in `categoryColor`. Multiply alpha along an alias chain. Reject invalid values, missing references, unknown fields, and cycles.

Each `categoryColor` entry is an opaque `#RRGGBB` value. `color.chip<Category>Wash` is the category color at 0.16 alpha. A chip uses `color.text` as its label and that wash over `color.panel` as its background. Other chip substrates require additional checks. Do not treat category color alone as a state indicator.

## Contrast checks

Every item in `contrast` has exactly `foreground`, `background`, `kind`, and optionally `underlay`. Color names follow the reference rules above. If the background has alpha below 1, `underlay` is required and must resolve to an opaque color. Composite the background over the underlay, then composite the foreground over that resulting opaque color. Composite the sRGB channel values, then linearize for WCAG relative luminance. Reject unresolved references, transparent final backgrounds, unrecognized fields or kinds, and failing checks.

Thresholds: `normal` >= 4.5:1; `large` and `mark` >= 3:1. Do not round before comparison. This list covers the named combinations, not every possible usage: any new foreground/background/layer stack in either app requires an additional check. In particular, normal `textMuted` on `raised` is not permitted; use `text`. Disabled `textFaint` is not approved as ordinary informational text.

## Output and migration

The generator may emit resolved colors and platform-specific sizes/type styles, retaining alpha where necessary. Preserve color names and document generated files. Validate the complete input before writing outputs; failures must not leave partial output. App theme replacement and visual results need review. Any extension of the top-level schema must bump `version`; do not silently ignore new top-level fields.
