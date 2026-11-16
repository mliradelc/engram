# Gemini review — adjudication against the code

Reviewed: 2026-11-16. Every claim checked against `codec_classifier.py` and probed on the
production image (ffmpeg 5.1.9, x265 3.5, SVT-AV1 3.1.x).

## Verdicts

| # | Claim | Verdict | Action |
|---|---|---|---|
| 1 | x265 missing `no-sao=1:strong-intra-smoothing=0` | **CORRECT — verified** | FIX |
| 2 | x265 preset `medium` too fast for grainy; use `slow` | **PARTIAL** | FIX (medium→slow for grainy BD) |
| 3 | Interlaced AV1 branch lacks a deinterlace filter | **WRONG — already present** | none |
| 4 | SVT preset 8 at UHD is a mistake; cap at 6 | **CORRECT** | FIX |
| 5 | 1080p clean CRF 25 too aggressive; use 23–24 | **DEFENSIBLE** | FIX to 24 |
| 6 | `film-grain=4` with denoise=0 stacks grain | **CORRECT as description, but intended** | none (document) |
| 7 | 4K DV Profile 7 needs dovi_tool | **CORRECT** | none (already documented) |
| 8 | "Keep" list: SD/HD CRF offset, luma-bias, tiles, thread caps | **MOSTLY WRONG about what the code does** | none |

## 1. x265 SAO / strong-intra-smoothing — CORRECT, and the most important finding

Probed on the production image. x265 3.5 default tool list:

```
x265 [info]: tools: b-intra strong-intra-smoothing deblock sao
```

With the proposed overrides:

```
x265 [info]: tools: b-intra deblock
```

So both filters **are** active in the current grainy plan and the overrides do remove them.
This directly undermines the routing logic: the whole point of sending grainy film to x265 is
grain retention, and SAO + strong-intra-smoothing are smoothing filters. **Adopting.**

Note the second line of the tools output also carries `aq-mode`; the current plan already sets
`aq-mode=3`, which is independent and stays.

## 2. Preset `medium` vs `slow` on grainy content — PARTIAL

Gemini says `fast` is used and disables RDO. That is **wrong**: the code emits
`"fast" if res == "uhd" else "medium"`, so 1080p grainy already gets `medium`, not `fast`.

The underlying advice — `medium` is the floor, `slow` is better for grainy Blu-ray — is sound
and matches the community position (JET: use the slowest preset you can afford). But it costs
time, and this host is an i3-7100T. **Adopting `slow` for 1080p grainy BD only**, keeping
`medium` for SD (where the pixel count is small and `slow` is affordable but unnecessary) and
keeping `fast` for UHD (where `slow` would be absurd on this silicon). This is a judgement
call the principal may want to revisit after the first measured encode.

## 3. Interlaced AV1 branch lacks a deinterlace filter — WRONG

`deinterlaced_plan()` sets:

```python
video_filters=["bwdif=mode=send_frame:parity=auto:deint=interlaced"]
```

and `video_args()` emits `-vf` whenever `video_filters` is non-empty. The filter is present.
Gemini appears to have inferred its absence from the class table rather than reading the code.

Its *adjacent* point is right and already handled: `interlaced` is **passthrough by default**,
and the AV1 path only runs when `ENGRAM_CODEC_INTERLACED=deinterlace` is set explicitly. The
3:2 hard-telecine warning is also correct — and is exactly why passthrough is the default.

## 4. SVT preset 8 at UHD — CORRECT

`preset = "8" if res == "uhd" else "6"`. At 4K, preset 8 drops enough partition/RDO search that
x265 medium can beat it on efficiency. **Cap UHD at preset 6.** The grain-synthesis suppression
that was tied to `preset == "6"` must be reworked, since UHD now also uses 6.

## 5. 1080p clean CRF 25 → 24 — DEFENSIBLE, adopting

Gemini is right that 25 with `luminance-qp-bias=25` is aggressive for archival 1080p Blu-ray.
The earlier research put the honest band at 23–28, so 24 sits comfortably inside it and costs
little. **Adopting 24 for HD clean.**

## 6. `film-grain=4` with `denoise=0` — correct description, intended behaviour

`film-grain-denoise=0` means SVT codes the original grainy frames *and* signals a grain model,
so the decoder superimposes synthetic grain on real grain. That is a real effect. It is also
deliberate: the alternative (`denoise=1`) runs SVT's denoiser, which JET calls poor and which
removes fine detail. At `film-grain=4` the synthetic layer is subtle and acts as a dither.
**No change**; the docstring already explains this.

## 7. Dolby Vision Profile 7 — CORRECT, already documented

The module does not attempt DV. The earlier plan document and the skill both say to keep UHD as
HEVC remuxes and that P7 needs `dovi_tool`. No change.

## 8. The "keep" list — mostly describes things the code does not do

- *"SD vs HD CRF offset (20 sd / 21 and 23 sd / 25)"* — the code does this (`crf = 20 if res == "sd" else 21`). Correct.
- *"`luminance-qp-bias=25` (SDR only)"* — correct, and `validate_plan()` actively rejects it on HDR.
- *"tile-columns 1 HD / 2 UHD / omitted SD … maps to 1/2/4 columns"* — **wrong on the arithmetic.** `tile-columns` is log2: 1 → 2 columns, 2 → 4 columns. Gemini's own parenthetical ("1 column (SD), 2 columns (1080p), 4 columns (4K)") contradicts its own values, and its claim that SD omits tiles is right but for the wrong reason. The code's `1` at HD and `2` at UHD is correct.
- *"Thread caps (`lp=4` / `pools=4`)"* — correct, both are emitted.

## Changes applied

1. x265: add `no-sao=1:strong-intra-smoothing=0` to required params (not optional — they must
   not be dropped by the fallback path).
2. x265 grainy preset: `slow` for HD, `medium` for SD, `fast` for UHD.
3. SVT UHD preset: 8 → 6, and decouple the grain-synthesis rule from the preset number.
4. Clean HD CRF: 25 → 24.
5. Tests updated for each, plus a regression test asserting the anti-smoothing params are
   present on every x265 plan.
