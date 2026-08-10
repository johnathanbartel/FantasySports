# Fantasy Sports Company

Internal fantasy sports platform. Football module (NCAAF + NFL) is the current
build target; baseball uses a related but distinct locked-rotation mechanic
and is out of scope for now.

## Core idea

Standard fantasy football double-counts touchdowns (QB gets the full passing
TD, the receiver gets the full receiving TD) and rewards yardage that never
actually produced points. This platform scores players on **Expected Points
Added (EPA)** instead: every play's value is the change in a field-position-
based Expected Points model, split among the players involved in that specific
play. See `Football/README.md` for the full scoring spec.

## Layout

```
Football/
  common/        shared EPA model + credit-attribution logic (used by both College and Professional)
  College/       NCAAF data pipeline (CollegeFootballData.com)
  Professional/  NFL data pipeline (nflfastR) - not started yet
Baseball/        out of scope for v1
Basketball/      out of scope for v1
Hockey/          out of scope for v1
```

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   # then fill in your API keys
```
