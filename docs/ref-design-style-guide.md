# BluePopcorn — Branding & Style Guide

## Brand Identity

**Name:** BluePopcorn
**Tagline:** Smart media requests for Seerr
**Logo:** Blue popcorn kernel/bucket — simplified geometric form that works at 16px (favicon) through hero size. Neon glow treatment for display usage, flat/solid variant for small sizes.

## Design Philosophy

The aesthetic is **"cinema at night, lit by neon blue."** Dark, atmospheric, cinematic. The blue is the light source — it illuminates everything around it. Warm amber accents provide grounding warmth (popcorn, butter, concession glow). The experience should feel like walking into a dark movie theatre where the only light is a vivid blue screen.

**Principles:**
- **Blue is the light, not a color swatch** — The CMYK neon blue isn't applied as a background or fill. It's a light source. Things are lit BY it. Surfaces catch it. Shadows fall away from it.
- **Blue dominates, warm accents ground it** — The neon blue is the primary identity. Warm amber (`#FFB800`) is the supporting warmth — popcorn, butter, warm light. Never equal billing — blue leads, amber supports.
- **Dark is the canvas** — The brand lives on dark backgrounds. Near-black is not a "dark mode" — it's the only mode. It's a movie theatre. The lights are off.
- **Glow is selective** — Neon glow is powerful BECAUSE it's rare. If everything glows, nothing does. Reserve glow for primary actions, headings, and the hero. Content stays clean.
- **Cinema atmosphere, not cinema cliche** — No film strips as borders, no clapperboards, no "LIGHTS CAMERA ACTION." The popcorn and the blue light tell the story.

## Color Palette

### Background

| Token | Hex | Usage |
|---|---|---|
| `--bg` | `#070B1A` | Page background — deep dark blue-black |
| `--surface` | `#0F1525` | Elevated surfaces, card backgrounds |
| `--border` | `#1A2340` | Borders, dividers |
| `--surface-warm` | `#131018` | Alternate surface with slight warm tint |

### Neon Blue (Primary — CMYK-inspired)

| Token | Hex | Usage |
|---|---|---|
| `--neon` | `#00D4FF` | THE blue. Primary interactive elements, key light source |
| `--neon-bright` | `#4DE8FF` | Hover states, emphasized text, active elements |
| `--neon-dim` | `#0099CC` | Pressed states, secondary blue accents |
| `--neon-glow` | `rgba(0, 212, 255, 0.4)` | Box-shadow / text-shadow glow halo |
| `--neon-wash` | `rgba(0, 212, 255, 0.06)` | Ambient blue light wash on nearby surfaces |

### Warm Accent (Popcorn / Amber)

| Token | Hex | Usage |
|---|---|---|
| `--amber` | `#FFB800` | Warm accent — popcorn highlights, secondary CTA |
| `--amber-bright` | `#FFD166` | Hover on warm elements |
| `--amber-dim` | `#CC9200` | Muted warm accent |
| `--amber-glow` | `rgba(255, 184, 0, 0.3)` | Warm glow halo |

### Text

| Token | Hex | Usage |
|---|---|---|
| `--text` | `#E8E4E0` | Primary text — warm off-white |
| `--text-dim` | `#8A837D` | Secondary text |
| `--text-faint` | `rgba(232, 228, 224, 0.06)` | Ghost text, watermarks |

## Typography

| Role | Font | Weight | Style |
|---|---|---|---|
| **Display / Headings** | Bebas Neue | 400 (only weight) | Uppercase, tracked. Movie poster DNA — tall, condensed, dramatic. The marquee font. |
| **Body text** | Geist Sans | 400, 500 | Vercel's workhorse. Clean, neutral, wide — contrasts perfectly with Bebas Neue's condensed drama. |
| **Code / Technical** | Geist Mono | 400, 500 | Clean monospace from the same family. Install commands, config, tool names. |

```css
font-family: 'Bebas Neue', 'Arial Narrow', sans-serif;     /* headings */
font-family: 'Geist', 'Inter', sans-serif;                  /* body */
font-family: 'Geist Mono', 'SF Mono', monospace;            /* code */
```

**Loading:** Bebas Neue from Google Fonts. Geist Sans + Geist Mono self-hosted or from `cdn.vercel.com/geist` (not on Google Fonts).

| **Neon sign / Logo** | HT Neon Regular | 400 | The 3D neon sign in the hero scene + logo wordmark. Actual neon tube letterforms. [Download](https://www.onlinewebfonts.com/download/cf984f65c2e069f0eed597bb0cee542d) |

**Never use:** Serif fonts, pixel fonts. The only script/display exception is HT Neon for the sign.

## UI Components

### Neon Text Treatment

```css
.neon-text {
    color: var(--neon);
    text-shadow:
        0 0 7px var(--neon-glow),
        0 0 20px rgba(0, 212, 255, 0.2),
        0 0 40px rgba(0, 212, 255, 0.1);
}
```

### Neon Flicker (use sparingly — one element max)

```css
@keyframes neon-flicker {
    0%, 19%, 21%, 23%, 25%, 54%, 56%, 100% {
        text-shadow: 0 0 8px var(--neon-glow), 0 0 30px var(--neon-glow);
        opacity: 1;
    }
    20%, 24%, 55% {
        text-shadow: none;
        opacity: 0.6;
    }
}
```

### Content Cards

```css
.card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    transition: border-color 0.2s, box-shadow 0.2s;
}
.card:hover {
    border-color: var(--neon-dim);
    box-shadow: 0 0 20px var(--neon-wash);
}
```

### Buttons

**Primary (neon blue):**
```css
.btn-primary {
    background: var(--neon);
    color: var(--bg);
    font-family: 'Bebas Neue', sans-serif;
    font-weight: 400; /* Bebas Neue only has 400 */
    text-transform: uppercase;
    letter-spacing: 0.08em;
    border: none;
    border-radius: 4px;
    box-shadow: 0 0 12px var(--neon-glow), 0 2px 8px rgba(0,0,0,0.3);
    transition: box-shadow 0.2s, background 0.2s;
}
.btn-primary:hover {
    background: var(--neon-bright);
    box-shadow: 0 0 20px var(--neon-glow), 0 0 40px rgba(0, 212, 255, 0.15);
}
```

**Secondary (amber):** Same structure with `--amber` family. Used for secondary actions.

**Ghost (outline):** Transparent background, `--neon` border and text, glow on hover.

### Install Command Block

```css
.install-block {
    background: rgba(7, 11, 26, 0.9);
    border: 1px solid var(--border);
    border-radius: 6px;
    font-family: 'Geist Mono', monospace;
    color: var(--text);
}
.install-block .prompt {
    color: var(--neon);
    text-shadow: 0 0 6px var(--neon-glow);
}
```

### Navigation

Minimal, dark, fixed top. Transparent over hero, solid on scroll:
```css
.nav {
    position: fixed;
    top: 0;
    width: 100%;
    z-index: 100;
    padding: 16px 24px;
    transition: background 0.3s;
}
.nav.scrolled {
    background: rgba(7, 11, 26, 0.95);
    backdrop-filter: blur(8px);
    border-bottom: 1px solid var(--border);
}
```

## Spacing & Layout

- Container max-width: 1100px
- Section padding: 80px vertical
- Card gap: 16px
- Border radius: 4-6px (modern, not bubbly)
- Hero: 100vh

## Tone of Voice

- **Casual, warm, movie-night energy.** Not corporate. Not overly technical. Recommending movies to a friend.
- **Brief.** Condensed headings reward short, punchy copy. "Search. Request. Watch." not "Seamlessly search for and request media content."
- **Cinema references welcome, but earned.** "Now Showing" works. "Coming Attractions" for upcoming features works. "LIGHTS CAMERA ACTION" does not.

## Don'ts

- Don't use film strip perforations, clapperboards, director's chairs, Hollywood stars
- Don't use red as an accent — the palette is blue + amber + dark
- Don't make everything glow — reserve glow for focal points
- Don't use light/white backgrounds anywhere — the experience is nocturnal
- Don't use rounded corners larger than 8px
- Don't add generic SaaS sections (testimonials, pricing tiers, partner logos)
- If it looks like a Tailwind dark template, start over

## File Naming

- OG image: `bluepopcorn-og.png`
- Logo: `logo.svg`, `logo-glow.svg`
- Favicon files: standard set (favicon.ico, favicon.svg, apple-touch-icon.png, etc.)
- Assets: lowercase, hyphenated
