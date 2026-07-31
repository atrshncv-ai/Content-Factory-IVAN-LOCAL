# Web parser

For `/plan`, research Google Trends, YouTube/Shorts, TikTok Creative Center and
public Instagram pages. Instagram is best-effort: its failure must be reported,
not bypassed with browser automation or invented data.

Return candidate objects accepted by `core.weekly_plan.CandidateStore`:
`source`, `title`, a real `http(s)` `url`, `published_at`, and `metrics`.
Include only metrics visible at the source; an empty metrics object is valid.
Never estimate missing metrics, URLs or dates. Aim for 10 distinct candidates;
stop below 5. Use the injected fetch/CLI adapter so failures remain isolated.

