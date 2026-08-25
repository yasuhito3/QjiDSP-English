# NOTICE

In addition to the original Qji code, this repository includes the following external assets and library dependencies.

## Bundled files

### `Musikvereinsaal_48k_tail.wav` (reverb impulse response)

This is the IR (impulse response) file referenced by the `real_hall_L` / `real_hall_R` filters in `spatial_final_v1-v6.yml`, modeling the reverb of the Musikverein (Vienna's Musikvereinsaal concert hall).

**Likely source**: an IR named "Musikvereinsaal" in [Voxengo's Free Reverb Impulse Responses](https://www.voxengo.com/impulses/)
pack (distributed by Aleksey Vaneev) has an exactly matching name. That pack was originally distributed as 44.1kHz/16-bit WAV,
and this file appears to have been resampled to 48kHz and trimmed to just the tail (the reverb decay portion) — though this is
inferred from the filename and hasn't been confirmed by comparing the actual waveforms.

> Voxengo's license terms (summarized): copyright to these impulse files belongs entirely to Aleksey Vaneev.
> Free use for any purpose, including commercial use, is permitted. However, selling these files themselves,
> or profiting directly or indirectly from their distribution, is not permitted.
>
> → Qji (free, open-source distribution on GitHub) is believed to satisfy these terms.
> Since the source is inferred rather than confirmed, comparing the waveforms directly, or checking with
> Voxengo directly, is recommended if you want certainty.

## Dependency licenses (for reference)

External libraries installed via pip etc. are governed by their own licenses. This repository's code only
`import`s or invokes them via subprocess — their source code itself is not bundled here.

| Library | License (reference) |
|---|---|
| CamillaDSP / pycamilladsp | MIT / GPLv3 (may vary by version — check the official repository) |
| ffmpeg | LGPL/GPL (varies by build configuration) |
| mutagen | GPLv2+ |
| yt-dlp | Unlicense (public domain equivalent) |
| ytmusicapi | MIT |
| Vosk | Apache-2.0 |
| deno | MIT |
| shairport-sync | multiple licenses (see the upstream repository for details) |

> `mutagen` is GPLv2+, but its source code is not bundled here — it's installed separately via pip.
> This is a common pattern among MIT-licensed Python music tools.

## License for Qji itself

See [LICENSE](./LICENSE) (currently a draft MIT license — please review and adjust the content as needed).
