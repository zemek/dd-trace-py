from ddtrace.trace import tracer


if __name__ == "__main__":
    assert tracer._span_aggregator.writer.intake_url == "http://172.10.0.1:8120"
    print("Test success")
