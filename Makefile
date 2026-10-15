# Kept for Unix users; run.py is the cross-platform interface (Windows, Actions, Claude Code).
.PHONY: data baseline test predict odds record-results
data: ; python run.py data
baseline: ; python run.py baseline
test: ; python run.py test
predict: ; python run.py predict
odds: ; python run.py odds --tag current
record-results: ; python run.py record-results
