.PHONY: install test check-config demo observe clean
install:
	python3 -m pip install -r requirements.txt
test:
	python3 -m unittest discover -s tests -v
check-config:
	python3 -m src.config
demo:
	EVENT_LOG_PATH=/tmp/metacore_demo.jsonl python3 examples/demo.py && \
	EVENT_LOG_PATH=/tmp/metacore_demo.jsonl python3 telemetry/observer.py; true
observe:
	python3 telemetry/observer.py
clean:
	rm -f /tmp/metacore_demo.jsonl
