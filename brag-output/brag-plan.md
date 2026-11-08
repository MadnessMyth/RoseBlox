# Roseblox brag plan

Made with /brag-slim (latent-spaces/brag). Format: landscape 1920×1080, 30 fps, 22.5 s.

## Answers

- **What is it?** A native Linux desktop launcher and installation manager for Roblox Player and Studio, running the macOS Roblox apps through Rosemary.
- **Who is it for?** Linux players (Arch Linux and CachyOS today) who want Roblox to feel like a normal desktop app.
- **What sets it apart?** It runs the desktop Roblox client, not the mobile one, and wraps it in a GTK/libadwaita app with the extras players want: performance switches, mods, Discord presence and, new in 0.4.2, side-by-side Players.
- **Most impressive claim:** Two Roblox Players side by side on Linux, one click each.
- **Visual hook:** The pink cube icon popping in with "Roblox on Linux / that feels native."
- **Real UI shown:** Home (Launch), Performance, Mods, While Playing, Player page with Duplicate.
- **Tone:** `app-store` with a bit of `default` punch: clean feature cards, smooth motion, upbeat.
- **Share caption:** see `share-copy.txt`.

## Angle

A sneak peek: "Roblox on Linux that feels native", then four quick proofs from the real app, ending on the newest feature (Duplicate) and where to get it.

## Visual identity

- Colors from the app: dark plum background (#16111A) with the app's magenta glow, accent pink (#E25C98), text #F6EEF4, muted #BEB0BC.
- Font: Cantarell (GNOME's UI font, the one the app uses).
- Real screenshots from `docs/screenshots`, shown as rounded cards with a soft shadow, a slow push-in, and animated highlights, cursor moves and clicks on top.
- The screenshots come from the test environment: paths like `/tmp/rbx-…` and versions like `version-0123456789abcdef` are fictional test data, not anyone's real system. No personal data appears.

## Music

Synthesised in `work/make_brag.py`. 120 BPM (beat = 0.5 s), A minor, chords Am–F–C–G (one per 2 s bar). Pad + pluck arpeggio in the hook; kick and bass enter on the reveal (2.0 s); hats and soft claps from the first highlight (5.0 s); final chord on the outro (19.0 s) and ring-out. Effects (cursor clicks, highlight ticks, whooshes into cuts) are pitched to the key and sit under the music on the same grid.

## Storyboard (every cut on a beat)

| # | Time | Beats | Scene | On screen | Motion / SFX |
|---|---|---|---|---|---|
| 1 | 0.0–2.0 | 4 | Hook | Icon, "Roblox on Linux", then "that feels native." | Icon pops on beat 0; line 2 on beat 1.5 |
| 2 | 2.0–5.0 | 6 | Reveal | ROSEBLOX · "Player and Studio, one click away" + Home | Cursor glides to Launch, click on 4.0 s (ripple + click) |
| 3 | 5.0–8.5 | 7 | Highlight: Performance | "Tune it like a native game" · Refresh rate · GameMode · MangoHud | Rows light up on beats (6.0, 6.5, 7.0) |
| 4 | 8.5–12.0 | 7 | Highlight: Mods | "Make Roblox yours" · Fonts · cursors · skyboxes · the classic oof | Rows light up on beats (9.5, 10.0, 10.5, 11.0) |
| 5 | 12.0–15.5 | 7 | Highlight: While playing | "Show friends what you're playing" · Discord presence · Join Server · server location | Rows light up on beats (13.0, 13.5, 14.0) |
| 6 | 15.5–19.0 | 7 | Highlight: New in 0.4.2 | "Two Players, side by side" · Duplicate an installation, launch both. | Click Duplicate… (16.5), copy appears (17.0), click Launch on the copy (18.0) |
| 7 | 19.0–22.5 | 7 | Outro | Icon, "Roseblox", github.com/MadnessMyth/RoseBlox, "Free and open source · Arch Linux and CachyOS", "Not affiliated with Roblox Corporation." | Final chord; fade to black over the last 0.5 s |

Transitions dip through the background: each scene fades its content out over the last 0.25 s before the cut, and the next scene builds in from empty background on the beat.
