# Frontend Style Guide â "FLOAT" system

Extracted from [`winsznx/float`](https://github.com/winsznx/float) (MIT, Â© 2026 FLOAT). The frontend lives in `apps/web` of a monorepo. This document is a complete spec for rebuilding the same look in a new project.

**The one-line description:** soft-brutalist fintech. A pale lavender canvas, no pure black or white anywhere, one confident violet "signal" accent, hard offset drop-shadows on bordered elements, tactile press-in/lift-up interactions, a film-grain overlay, and restrained GSAP motion (float, stagger-in, scroll-draw). Playful pastel mode-cards sit on top of an otherwise disciplined, quiet system.

This is a more sophisticated system than a typical single-file landing page: Tailwind v4 with a CSS-first `@theme`, per-component utility classes, and animation as a first-class concern. It shares the neo-brutalist *shadow* vocabulary with sticker/scrapbook designs, but the execution is calmer and more product-grade.

---

## 1. Stack

| Concern | Choice |
|---|---|
| Framework | Next.js App Router, React 19, TypeScript, monorepo (`apps/web`) |
| Styling | **Tailwind v4, CSS-first config** via `@theme` in `globals.css`. Utilities applied per-component; arbitrary values (`shadow-[6px_6px_0_0_...]`) used freely |
| Fonts | `next/font/google` â Space Grotesk (display), Inter (body), IBM Plex Mono (labels/data) |
| Motion | GSAP + ScrollTrigger for reveals and the thesis draw; raw `<canvas>` for the hero particle field; CSS transitions for hover |
| Icons | `lucide-react`, plus hand-authored inline SVG for mode glyphs |

**Architectural note vs. the hand-written-CSS approach:** here the design tokens are declared once in `@theme`, which makes each token available *both* as a Tailwind utility (`bg-signal`, `text-muted`) *and* as a plain variable (`var(--color-signal)`) for arbitrary values. That dual exposure is the whole trick â the brutalist shadows need the raw variable, the layout needs the utilities. Clone this pattern; don't fall back to a separate config file.

### Font setup

```tsx
// app/layout.tsx
import { Space_Grotesk, Inter, IBM_Plex_Mono } from "next/font/google";

const spaceGrotesk = Space_Grotesk({ variable: "--font-space-grotesk", weight: ["500","700"], display: "swap", subsets:["latin"] });
const inter        = Inter({ variable: "--font-inter", weight: ["400","500","600"], display: "swap", subsets:["latin"] });
const ibmPlexMono  = IBM_Plex_Mono({ variable: "--font-ibm-plex-mono", weight: ["400","500"], display: "swap", subsets:["latin"] });

<html className={`${spaceGrotesk.variable} ${inter.variable} ${ibmPlexMono.variable} h-full antialiased`}>
  <body className="min-h-full flex flex-col">
```

Weights are loaded narrowly on purpose â display only ships 500/700, body 400/500/600, mono 400/500. Don't request weights you won't use.

---

## 2. Color tokens

Declared in `globals.css` inside `@theme`. **Hard rule from the source: no pure white (`#FFF`) and no pure black (`#000`) anywhere.** Every "white" is a warm off-white, every "black" is a desaturated near-black violet.

```css
@theme {
  /* surfaces */
  --color-page:    #f3effa;  /* pale lavender â the app canvas */
  --color-surface: #fdfbfe;  /* near-white card surface */
  --color-void:    #1c1726;  /* near-black violet â borders, dark text, dark fills */
  --color-void-3:  #efe9f7;  /* muted lavender chip surface */

  /* the one accent */
  --color-signal:       #7c6cf5;              /* violet â CTAs, active state, links-that-matter */
  --color-signal-dim:   rgba(124,108,245,0.35);
  --color-signal-faint: rgba(124,108,245,0.12);

  /* text */
  --color-text:    #1c1726;
  --color-muted:   #6b6478;
  --color-muted-2: #948da3;

  /* lines */
  --color-border:        rgba(28,23,38,0.10);
  --color-border-strong: rgba(28,23,38,0.20);
  --color-brut-line:     rgba(28,23,38,0.88);  /* the offset-shadow color */

  /* pastel mode accents */
  --color-coral: #f2a683;
  --color-mint:  #b8e6a8;
  --color-lav:   #c9bfea;
}
```

Extra card fills used inline (not tokenized): `#DDD4FB` (pledge lavender), `#6b5ce0` (button hover â a darker signal).

### Rules for using color

- **One accent, used sparingly.** `--color-signal` (violet) is the only saturated brand color. It marks the primary CTA, the active nav pill, notification dots, and small emphasis swatches. Everything else is greyscale-violet. Resist adding a second accent.
- **`--color-void` is structural**, not just text: it's the color of *every* border (`border-void`) and the darker offset shadow. Borders are near-black, never grey, on emphasized elements.
- **The pastels (coral / mint / lav) are categorical**, not decorative â each of the four "modes" owns one color, and that mapping is consistent everywhere (cards, nav pills, floating chips). Don't scatter them for variety; they encode *which mode*.
- **`--color-brut-line`** (88% ink) is the default shadow color. Pure `--color-void` is used only where the shadow needs to read as fully solid (the `Swipe` inline badge).
- `::selection` is tinted `signal-faint`. Small touch, sets the tone.

---

## 3. Typography

Three faces, three jobs. This is a stricter split than most systems.

### Display â Space Grotesk, bold, tight

```
h1 / wordmark : clamp(52px,8vw,104px), weight 700, leading .94, tracking -.01em
big signoff   : clamp(72px,15vw,200px), weight 700, leading .9, tracking -.02em
h2 (sections) : clamp(28px,3.6â4vw,40â44px), weight 700, leading 1.2
card title    : 24px, weight 700
```

Space Grotesk is the personality face â geometric, slightly quirky, tech-forward. The wordmark "FLOAT" is set in it at every scale, from 16px nav to a 200px footer. Global `letter-spacing:-0.01em` is applied on `body`, and display headings tighten further.

### Body â Inter

```
lead paragraph : clamp(18px,2.2vw,24px), weight 400
body copy      : 13â15px, leading 1.55
```

Inter carries all sentence-level copy. Quiet, legible, no personality of its own â which is the point; it lets the display and mono faces do the talking.

### Labels & data â IBM Plex Mono, uppercase

The recognizable tell. Eyebrows, chip labels, trust items, chain names, "To/Sending" field labels, and the footer are all mono, 11â13px, uppercase, positive tracking:

```css
/* section eyebrow */
font: var(--font-mono); font-size:12px; text-transform:uppercase;
letter-spacing:0.18em; color:var(--color-muted);

/* trust strip / chain labels */
font-size:11â12px; letter-spacing:0.08em; uppercase; color:var(--color-muted-2);

/* handles (@ada.eth) */
font-size:12px; (not uppercased â handles stay lowercase)
```

Tracking is the signal: eyebrows get the widest (`.18em`), data labels tighter (`.08em`). Mono is also used un-uppercased for wallet addresses and handles, where it reads as "machine/identifier".

---

## 4. Layout

### Container

`max-w-[1180px]` centered, with generous section padding. There's no shared `.shell` class â it's `mx-auto max-w-[1180px] px-12` repeated per section (px-6 on the centered hero/thesis).

### Section rhythm

Vertical padding is asymmetric and overlapping â sections lead with more top than bottom or vice-versa to control how they meet:

```
hero      : pt-[100px] pb-[60px], min-h-screen, centered
modes     : pt-[70px]  pb-[90px]
problem   : pt-[60px]  pb-[90px]
thesis    : pt-[70px]  pb-[60px], centered
```

### Grids â lopsided by default

```
hero    : grid-cols-[1.05fr_0.95fr]   (copy slightly wider than demo)
problem : grid-cols-2, gap-20
modes   : grid-cols-2, gap-7
```

Single breakpoint for the whole marketing page: **`min-[900px]`**. Below it everything is one column, centered; above it, two columns, left-aligned. (In-app uses Tailwind's default scale.)

### App shell (authenticated area)

```tsx
<div className="relative flex min-h-full flex-1 flex-col bg-page">
  <GrainOverlay />          {/* fixed, z-50, over everything */}
  <TopBar />               {/* h-16, border-b, wordmark + bell + avatar */}
  <main className="flex-1 px-5 pb-10 pt-6">{children}</main>
</div>
```

---

## 5. The signature: offset shadows + tactile hover

This is the heart of the system. Bordered elements cast a **hard, un-blurred offset shadow** in the ink color, and interactions move the element *into or out of* that shadow. Two distinct behaviors â learn both:

### A) Buttons â press IN

The shadow *collapses to zero* as the element slides down-right into where the shadow was. Reads like physically pressing a key.

```html
<!-- primary CTA -->
<a class="rounded-full border-2 border-void bg-signal px-[30px] py-3.5
          text-void shadow-[5px_5px_0_0_var(--color-brut-line)]
          transition-all duration-150
          hover:translate-x-[5px] hover:translate-y-[5px] hover:scale-[0.98]
          hover:bg-[#6b5ce0] hover:shadow-[0_0_0_0_var(--color-brut-line)]
          focus-visible:ring-2 focus-visible:ring-[var(--color-signal)]">
  Continue with email
</a>
```

The translate distance **equals the shadow offset** (5px shadow â translate 5px), so the element lands exactly where its shadow was. Add `scale-[0.98]` for the extra "give". Nav's smaller button uses `3px/3px`. The secondary/ghost button uses the same mechanic with a `signal`-colored shadow instead of ink.

### B) Cards â lift UP

The opposite: the shadow *grows* and the card rises. Reads like peeling a sticker off the page.

```html
<div class="rounded-[20px] border-2 border-void bg-[#F2A683] rotate-[-1.6deg]
            p-9 shadow-[6px_6px_0_0_var(--color-brut-line)]
            transition-[rotate,translate,box-shadow] duration-200 ease-out
            hover:-translate-y-1.5 hover:rotate-[-0.7deg]
            hover:shadow-[9px_9px_0_0_var(--color-brut-line)]">
```

Cards also carry a small **resting rotation** (Â±1.3â1.6Â°) that *relaxes toward 0* on hover (`rotate-[-1.6deg]` â `hover:rotate-[-0.7deg]`). The tilt alternates per card so the grid looks hand-placed.

### Shadow offset scale

`3px` (nav button, chips, active pill, Swipe badge) Â· `5px` (hero CTAs) Â· `6pxâ9px` (mode cards) Â· `7px` (confirmation card). Offset is always symmetric (`Npx Npx`), always `0 0` blur/spread, always `--color-brut-line` or `--color-void`.

### Border-radius scale

Tokenized: `--radius-sm:8px Â· md:12px Â· lg:16px Â· xl:20px Â· 2xl:28px`. In practice: `rounded-full` for pills/chips/buttons, `rounded-[20px]`/`2xl` for cards, `rounded-[6px]` for the tiny Swipe badge.

---

## 6. Components

### Glassmorphic floating nav

Fixed, pill-shaped, centered, translucent with a heavy backdrop blur â floats over the hero rather than sitting in a bar.

```tsx
<nav className="flex items-center gap-8 rounded-full py-3 pl-[22px] pr-[14px]"
  style={{
    background: "rgba(250,247,253,0.65)",
    backdropFilter: "blur(20px) saturate(160%)",
    border: "0.5px solid var(--color-border-strong)",
  }}>
```

`0.5px` hairline borders (via `border-[0.5px]` / inline) appear wherever a divider should be barely-there â the nav edge and the trust strip's `border-y`.

### Mode card

See Â§5B. Anatomy: an inline SVG glyph absolutely positioned top-right (each rotated a few degrees), a mono uppercase eyebrow in `signal`, a Space-Grotesk title, and an Inter subline at `text-void/75`. The whole inner block starts at `opacity-0` and is revealed by ScrollTrigger.

### Floating handle chips (hero ambient layer)

Pastel pills with handles (`@ada.eth`), absolutely positioned across the hero, each bobbing on its own GSAP sine loop. `pointer-events-none`, `aria-hidden`, hidden below 900px.

```html
<span class="absolute rounded-full border-2 border-void bg-coral px-4 py-[7px]
             font-mono text-[12px] text-void"
      style="box-shadow:3px 3px 0 0 var(--color-brut-line)">@ada.eth</span>
```

### Swipe (inline emphasis badge)

The system's version of a highlighter â wraps 2â3 words inside a headline in a tilted signal-filled sticker.

```tsx
<span className="inline-block rounded-[6px] border-2 border-void bg-signal px-3 py-0.5 text-void"
      style={{ boxShadow: "3px 3px 0 0 var(--color-void)", transform: "rotate(-1deg)" }}>
  {children}
</span>
```

Used as `Your money. Any chain. <Swipe>Just send.</Swipe>`.

### Mode pill switcher (in-app nav)

Horizontal scrollable row. Active pill gets its mode color + border-void + 3px shadow; inactive pills are flat `bg-surface` with a faint border and muted text.

```tsx
isActive
  ? `border-void text-void shadow-[3px_3px_0_0_var(--color-brut-line)] ${bgClass}`
  : "border-border bg-surface text-muted hover:text-text"
```

### Confirmation card (in-app)

The workhorse surface: `rounded-2xl border-2 border-void bg-surface p-8 shadow-[7px_7px_0_0_var(--color-brut-line)]`. Inside, mono uppercase micro-labels ("SENDING", "TO") sit above Inter/Space-Grotesk values. Addresses render in `font-mono text-[11px] text-muted-2` beneath the human label â machine truth under the friendly name.

### Amount input

Large centered Space-Grotesk numerals with a `$` prefix, transparent background, focus ring in `coral`. Input is regex-guarded to two decimals. Quick-select chips (25/50/100) below.

### Top bar

`h-16`, `border-b border-border`, wordmark link left, notification bell + avatar right. Avatar is a `border-2 border-void` circle that lifts `-translate-y-0.5` on hover. Unread state is a tiny `bg-signal` dot, never a number badge on the icon itself.

---

## 7. Motion

Motion is deliberate and layered. Every animated component checks `prefers-reduced-motion` and provides a static end-state. Five distinct techniques:

1. **Hero stagger-in (GSAP, on load).** Elements marked `data-hero-in` start at `opacity:0, y:20` and animate up in sequence â `duration .6, ease power3.out, stagger .1, delay .1`.

2. **Scroll reveals (GSAP ScrollTrigger).** Cards and rows start `opacity:0, y:16â24` and rise when they hit `top 85â90%` of the viewport, with per-item delay (`i * 0.06â0.1`). `document.fonts.ready` triggers a `ScrollTrigger.refresh()` so positions are correct after webfonts load.

3. **Particle field (raw canvas).** 150 drifting dots on the hero; lines drawn between the â¤3 nearest neighbors within 150px, opacity fading with distance, in translucent signal-violet. DPR-scaled. Reduced-motion â a single static frame.

4. **Ambient float loops (GSAP).** Handle chips each bob `y:+=11` on an infinite `sine.inOut yoyo` with staggered per-chip durations (2.6â3.3s) â no two in sync.

5. **Scroll-drawn diagram (GSAP + SVG).** The "thesis" section builds an SVG of six chains connecting to a center point: gradient stroke paths drawn via `stroke-dashoffset` animation, then `animateMotion` particles travel each path, then the center node pops in with `back.out(2)`. Fires once on scroll-in.

Hover interactions (Â§5) are plain CSS transitions, `duration-150â200`. **The rule: JS/GSAP for entrances and ambient life, CSS for interaction.**

---

## 8. Texture: film grain

A fixed, full-screen SVG-noise overlay sits above everything at very low opacity â it's what keeps the flat pastel surfaces from looking sterile.

```tsx
<div aria-hidden className="pointer-events-none fixed inset-0 z-50 h-full w-full
     opacity-[0.035] mix-blend-overlay">
  <svg className="h-full w-full">
    <filter id="float-grain">
      <feTurbulence type="fractalNoise" baseFrequency="0.85" numOctaves="2" stitchTiles="stitch" />
      <feColorMatrix type="saturate" values="0" />
    </filter>
    <rect width="100%" height="100%" filter="url(#float-grain)" />
  </svg>
</div>
```

`opacity 0.035` + `mix-blend-overlay` is the whole recipe. Barely perceptible; removing it makes the design look noticeably flatter.

---

## 9. Responsive

Marketing pages use a **single custom breakpoint, `min-[900px]`**: one centered column below, two-column left-aligned layout above. Ambient decoration (chips) is `hidden min-[900px]:block` â dropped entirely on mobile rather than repositioned. A secondary `min-[480px]` switches stacked CTAs to a row.

In-app screens use Tailwind's default breakpoint scale and are built mobile-first (`px-5` gutters, scrollable pill rows).

Type scales via `clamp()` rather than breakpoint swaps, so headings resize continuously.

---

## 10. Accessibility floor

Consistently present in the source â copy it:

- `prefers-reduced-motion` guard in **every** animated component, each with a defined static end-state (not just "no animation" â the correct final frame).
- `focus-visible:outline-none focus-visible:ring-2` with a `signal`/`signal-dim`/`coral` ring on every interactive element. Focus is never removed without replacement.
- `aria-hidden` on all decoration (canvas, grain, chips, mode glyphs).
- `aria-label` on icon-only controls (bell with unread count, avatar link, wordmark home link).
- Semantic `<header>`/`<nav>`/`<section>`/`<footer>`, real `<Link>`s for navigation.
- No-pure-black text keeps contrast intentional (`#1c1726` on `#f3effa`), not maxed.

---

## 11. Copy voice

The writing is terse, confident, and slightly poetic â it matters as much as the type.

- **Three-beat taglines with a fragment ending:** "Your money. Any chain. Just send." / "One line. Any chain." The last beat is what goes in the `Swipe` badge.
- **Eyebrows name the concept in mono caps:** `THE FOUR FLOWS`, `UXMAXX Â· PARTICLE Â· ARBITRUM Â· MAGIC`. Dot-separated (`&middot;`) partner/tech lists are a recurring device.
- **Mode sublines are one plain sentence, verb-forward:** "Type a name. FLOAT finds the wallet." / "Everyone settles from what they have." No feature-speak.
- **Problem statements are concrete and slightly rueful:** "Copy the wrong address, lose the money." / "Splitting a bill turns into a spreadsheet nobody opens again."
- **In-app labels are plainspoken system words** in mono caps: `SENDING`, `TO`. The confirmation flow shows the human name *and* the raw address â honest about what's irreversible.
- **Sign-off lines have attitude:** "Crypto has the infrastructure. FLOAT uses it."
- Sentence case in body, UPPERCASE only in mono labels. Handles stay lowercase.

---

## 12. Build checklist

1. `globals.css`: `@import "tailwindcss";` then the `@theme` block with the full token set (Â§2). Add the `body` base (page bg, text color, Inter, `letter-spacing:-0.01em`, antialiasing) and the `::selection` tint.
2. `layout.tsx`: load the three Google fonts as CSS variables with narrow weight sets; put the variable classes + `antialiased` on `<html>`.
3. Enforce the **no-pure-black/white** rule from the first color you place.
4. Build the glassmorphic floating nav first â it establishes the blur + hairline-border + pill language.
5. Hero: `min-h-screen` centered, lopsided grid, mono eyebrow â giant Space-Grotesk wordmark â lead line with one `Swipe` badge â two CTAs (press-in). Layer the particle canvas and floating chips behind at low z, both `aria-hidden` and reduced-motion-safe.
6. Give every emphasized element the recipe: `border-2 border-void`, hard offset shadow in `brut-line`, and the correct interaction â **buttons press in, cards lift up**.
7. Categorical color: assign each feature/mode one pastel and keep that mapping everywhere.
8. Add GSAP reveals (`data-*` hooks, ScrollTrigger, `fonts.ready` refresh) â always with a reduced-motion static branch.
9. Drop the fixed grain overlay last (`z-50`, `opacity 0.035`, `mix-blend-overlay`).
10. `focus-visible` ring + `aria-hidden` on decoration as you go, not after.

### The two hover recipes, side by side

```css
/* PRESS IN â buttons, links, active pills */
shadow-[5px_5px_0_0_var(--color-brut-line)]
hover:translate-x-[5px] hover:translate-y-[5px] hover:scale-[0.98]
hover:shadow-[0_0_0_0_var(--color-brut-line)]

/* LIFT UP â cards, avatar */
shadow-[6px_6px_0_0_var(--color-brut-line)]
hover:-translate-y-1.5 hover:shadow-[9px_9px_0_0_var(--color-brut-line)]
```

---

## 13. What to change when reusing

Keep the grammar, swap the specifics â otherwise the clone reads as the same product:

| Keep | Change |
|---|---|
| No pure black / no pure white; near-black-violet ink | The lavender page + violet signal (pick your own single accent; keep it *single*) |
| Offset-shadow + press-in/lift-up interaction pair | The exact offsets if you want a softer or harder feel |
| Space Grotesk / Inter / IBM Plex Mono three-role split | The specific faces (keep the display + body + mono *structure*) |
| Categorical pastel-per-feature mapping | Which features, which pastels, how many |
| Mono uppercase eyebrows with wide tracking | The literal label text |
| Grain overlay + GSAP reveals + a canvas/SVG hero moment | The hero's *subject* â particles suit a network product; pick a moment from your own domain |
| `prefers-reduced-motion` + focus-ring floor | â (never drop these) |

The single most copy-able idea here is the **discipline**: one accent, three fonts with strict jobs, no pure black or white, and a *consistent* physics for how things respond to touch. The pastels and the grain are what make it feel warm instead of corporate â but they sit on top of a very controlled base.
