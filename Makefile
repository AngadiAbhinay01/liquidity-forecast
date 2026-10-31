PY ?= python3

.PHONY: install test starter full detect validate reproduce

install:
	$(PY) -m pip install -r requirements.txt

test:
	$(PY) -m pytest -q

starter:
	$(PY) -m src.generate --scale starter --out data/starter

full:
	$(PY) -m src.generate --scale full --out data/full

detect:
	$(PY) -m src.detect --data data/full && $(PY) -m src.detect.evaluate --data data/full

validate:
	-$(PY) -m src.validate --data data/full --compare-ground-truth

reproduce:
	$(PY) -m src.reproduce --scale full
