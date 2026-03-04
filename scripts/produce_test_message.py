#!/usr/bin/env python3
"""Produce one test message to thumbnail-image-upload-stream for local testing."""
import json
import os
import sys

# Add project root so we can use kafka_factory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Use local config
os.environ.setdefault("KAFKA_CONFIG_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.local.yaml"))

def main():
    from kafka_factory import get_config, _base_config
    from confluent_kafka import Producer

    cfg = get_config()
    bootstrap = cfg.get("bootstrap_servers", "localhost:9092")
    broker_type = cfg.get("broker_type", "plain")
    topic = cfg.get("consumer", {}).get("topic", "thumbnail-image-upload-stream")

    conf = _base_config(bootstrap, broker_type)
    producer = Producer(conf)

    # Minimal ThumbnailRequest-shaped payload (use a real WSI URL to test full /process success)
    payload = {
        "id": "local-test-1",
        "created_on": "2025-02-25T12:00:00Z",
        "image_bucket_link": "https://storage.googleapis.com/wsi_viewer_test/1087-25.svs",
        "patient_id": "P1",
        "slide_id": "S1",
        "block_id": "B1",
    }
    producer.produce(topic, value=json.dumps(payload).encode("utf-8"))
    producer.flush(timeout=10)
    print(f"Produced test message to {topic}: id={payload['id']}")

if __name__ == "__main__":
    main()
