# Project Plan: Streaming Catalog Analytics (Netflix, Prime Video, Apple TV+)

**Question addressed:** Provide a detailed (tentative) plan for each stage of the analytics pipeline, specifying data sources and the specific goals of the analysis.

**Research focus:** How do Netflix, Amazon Prime Video, and Apple TV+ differ in the size, composition, and quality of their US catalogs — and specifically, how does each platform's mix of in-house (original) vs. third-party (licensed) content compare?

---

## 1. Data Collection

**Source:** [The Movie Database (TMDB)](https://www.themoviedb.org/) API (v3), specifically:
- `/discover/movie` and `/discover/tv` — filtered by `with_watch_providers`, `watch_region=US`, and `watch_monetization_types=flatrate` — to enumerate every title available on each platform's subscription tier.
- `/movie/{id}` and `/tv/{id}` (with `append_to_response=watch/providers,external_ids,release_dates`) — for per-title metadata: production companies/networks, genres, runtime, cast-level release notes, ratings, and IMDb cross-reference IDs.

**Scope decisions:**
- **Platforms:** Netflix, Amazon Prime Video, Apple TV+ (provider IDs 8, 9/119, and 350 respectively).
- **Region:** US only, since watch-provider availability and pricing tiers are region-specific.
- **Time window:** titles released (movies) or first aired (TV) in the **last 5 years**, to keep the dataset current and a manageable size (~12,000 rows vs. ~34,000 unfiltered).
- **Monetization:** `flatrate` (subscription-included) only — excludes rentals/purchases, which aren't part of the "what's in my subscription" question.

**Goal of this stage:** produce a complete, deduplicated list of title IDs per platform/media-type combination, and pull full metadata for each one exactly once (cached locally so repeated runs don't re-fetch).

**Implementation:** [`init.py`](init.py) — handles pagination beyond TMDB's 10,000-result cap by splitting large queries into date-bounded sub-queries, retries on transient network errors, and caches every detail response to disk so the script is resumable.

**Known limitations to carry forward:**
- TMDB reflects **current** availability — it cannot reconstruct a platform's catalog as it existed historically.
- Vote counts/ratings come from TMDB's own user base, which is smaller and skews differently than IMDb or Rotten Tomatoes.

---

## 2. Data Labeling & Enrichment

**Goal:** classify every title as **in-house** (produced by the platform) or **third-party** (licensed), since this distinction is central to the research question.

**Method:**
1. **Studio/network match** — a title is in-house if the platform's studio name (e.g., "Netflix") appears in `production_companies` (movies) or `networks` (TV).
2. **Premiere-date fallback (movies only)** — TMDB frequently omits the streamer from `production_companies` on its own films (e.g., *Red Notice*). A movie is also labeled in-house if a US release-date note names the platform within 90 days of the title's first US release.

**Optional future enrichment:** join OMDb (`imdb_id` → IMDb/Rotten Tomatoes ratings) for a second, independently-sourced quality signal, since TMDB's own vote counts are thin for most titles (see Section 3).

**Goal of this stage:** a `label` column (plus `studio_match`/`platform_premiere` flags for transparency) that supports both a strict "produced by" definition and a broader "marketed as an Original" definition, so downstream analysis isn't locked into one interpretation.

---

## 3. Data Cleaning & Validation

**Goal:** turn the raw API pulls into a trustworthy analysis table and document exactly where it can and can't be trusted.

**Planned checks:**
- **Missingness by field and media type** — `budget`/`revenue` are movie-only and mostly unknown (~64% missing); `networks`/`number_of_seasons` are TV-only; `runtime` is often blank for TV. Each metric's usable sample size will be reported alongside any result that uses it.
- **Zero-as-missing correction** — TMDB uses `0` for unknown budget/revenue; these are recoded to missing rather than treated as true zeros.
- **Low vote-count problem** — ~77% of titles have fewer than 50 votes, so raw `vote_average` is unreliable for most of the catalog. Plan: either filter to a minimum vote threshold or compute a Bayesian-shrunk **weighted rating** (IMDb-style) that pulls low-vote titles toward the global mean.
- **Duplicate/overlap handling** — a title on multiple platforms appears once per platform (by design, since the platform is the unit of comparison), but title-level statistics will dedupe on `(media_type, tmdb_id)`.
- **Provider-name normalization** — TMDB lists ad-supported tiers and channel add-ons as separate provider strings (e.g., "Netflix Standard with Ads," "AMC+ Amazon Channel"); these are collapsed to their underlying service before counting cross-platform availability.

**Implementation:** `data/titles.csv` is the cleaned output; the normalization/derivation logic lives in [`eda.ipynb`](eda.ipynb) Section 1 so raw and derived fields stay traceable to their source.

---

## 4. Exploratory Data Analysis

**Goal:** build a working understanding of the dataset's shape and surface the patterns worth testing formally, before drawing conclusions.

**Planned analyses** (implemented in [`eda.ipynb`](eda.ipynb)):
1. **Catalog composition** — in-house vs. third-party share, by platform and media type.
2. **Rating distributions** — with the low-vote-count caveat addressed via filtering/weighting (Section 3).
3. **In-house vs. third-party quality comparison** — non-parametric group comparison (Mann-Whitney U, since ratings are bounded and skewed), reported per platform/media type with an effect size (probability a random original outrates a random licensed title).
4. **Genre mix** — which genres each platform emphasizes, and whether genre emphasis differs between what a platform makes vs. what it licenses.
5. **Release timing** — quarterly release volume by platform and source, with an explicit caveat about right-censoring (licensed titles typically arrive on a platform months to years after original release, so the most recent quarters will look artificially thin).
6. **Runtime, season count, and language mix** — structural differences in content format across platforms.
7. **Cross-platform overlap** — how many services carry each title, and whether in-house content is more exclusive than licensed content.
8. **Correlation structure** — relationships among ratings, vote count, popularity, budget, and in-house status (Spearman, given skewed distributions).

**Goal of this stage:** a documented, reproducible notebook that a reader can audit end-to-end — every chart paired with the caveat needed to interpret it correctly (sample size, censoring, confound).

---

## 5. Statistical Analysis / Modeling (Next Stage)

**Tentative goals**, contingent on what EDA surfaces as worth formalizing:
- **Hypothesis testing** — formalize the "are originals rated better?" comparisons from EDA with appropriate corrections for multiple comparisons across platform/media-type combinations.
- **Regression** — model `vote_average` (or the weighted rating) as a function of platform, in-house status, genre, runtime, and release recency, to isolate the in-house effect from confounds like genre mix and vote-count bias.
- **Classification (stretch goal)** — predict a title's likely platform-fit or in-house status from its metadata, as a way of quantifying what makes each platform's originals distinctive.

**Constraint to plan around:** budget/revenue are too sparse (~64% missing on movies, unavailable for TV) to serve as a primary predictor; any model relying on them will need to either restrict to the subset with known values or exclude them and note the limitation.

---

## 6. Visualization & Reporting

**Goal:** communicate findings to a non-technical audience (e.g., a course deliverable or presentation) without requiring the reader to run code.

**Plan:**
- Finalize a small set of publication-quality charts from the EDA notebook (catalog composition, rating comparison, genre heatmap) using a consistent, colorblind-safe palette.
- Summarize each finding with its supporting caveat directly alongside it (e.g., "Netflix's licensed TV rates higher than its originals — but licensed shows are pre-vetted hits, so this is descriptive, not causal").
- Package the final narrative as a short written report and/or slide deck, separate from the analysis notebook, so the audience-facing deliverable doesn't require reading code.

---

## 7. Timeline (Tentative)

| Stage | Status |
|---|---|
| Data collection ([`init.py`](init.py)) | Complete — `data/titles.csv`, ~12,450 rows |
| Labeling & cleaning | Complete — in-house/third-party rule, missingness documented |
| Exploratory analysis ([`eda.ipynb`](eda.ipynb)) | Complete (first pass) — composition, ratings, genres, timing, overlap |
| OMDb enrichment | Not started — optional, pending an OMDb API key |
| Statistical modeling | Not started |
| Final report / presentation | Not started |

---

## 8. Key Risks & Limitations to Carry Through Every Stage

- **Snapshot bias:** availability data is current, not historical — no platform's catalog trend can be reconstructed further back than TMDB's release-date metadata allows.
- **Vote-count sparsity:** most titles have too few TMDB votes for their rating to be meaningful individually; every rating-based conclusion must state its effective sample size.
- **Labeling ambiguity:** "in-house" mixes true originals with acquired films marketed as Originals; any headline claim should specify which definition (`studio_match` vs. the broader premiere-based label) it uses.
- **Confounding in quality comparisons:** licensed catalogs are curated for proven popularity, which biases naive in-house vs. third-party rating comparisons; this needs to be stated as a limitation, not resolved away, unless the modeling stage explicitly controls for it.

