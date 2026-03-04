"""
Kafka client factory. Loads broker config from config.yaml and creates consumer/producer
with broker type switchable (e.g. gcp_managed with OAuth vs plain for local).
"""
import os
import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# Default config path; allow override via env
CONFIG_PATH = os.environ.get("KAFKA_CONFIG_PATH", "config.yaml")
_CONFIG_CACHE = None


def _load_config():
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    path = Path(CONFIG_PATH)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    if not path.exists():
        raise FileNotFoundError(f"Kafka config not found: {path}")
    with open(path) as f:
        _CONFIG_CACHE = yaml.safe_load(f)
    return _CONFIG_CACHE


def _gcp_oauth_cb(oauth_config):
    """OAuth callback for GCP Managed Kafka using Application Default Credentials."""
    import time
    from google.auth import default
    from google.auth.transport.requests import Request

    credentials, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    if not credentials.valid:
        credentials.refresh(Request())
    token = credentials.token
    # librdkafka expects expiry as Unix timestamp in seconds
    expiry_seconds = int(credentials.expiry.timestamp()) if credentials.expiry else int(time.time()) + 3600
    return token, expiry_seconds


def _base_config(bootstrap_servers: str, broker_type: str):
    """Build common config; add SASL/OAuth for gcp_managed."""
    conf = {
        "bootstrap.servers": bootstrap_servers,
    }
    if broker_type == "gcp_managed":
        conf.update({
            "security.protocol": "SASL_SSL",
            "sasl.mechanisms": "OAUTHBEARER",
            "oauth_cb": _gcp_oauth_cb,
        })
    # else "plain" or others: no SASL
    return conf


def get_config():
    """Return the kafka section of config (for topics, group_id, etc.)."""
    cfg = _load_config()
    return cfg.get("kafka", {})


def create_consumer():
    """Create a Kafka consumer. Manual commit only; caller controls commit."""
    from confluent_kafka import Consumer

    kafka_cfg = get_config()
    broker_type = kafka_cfg.get("broker_type", "plain")
    bootstrap = kafka_cfg.get("bootstrap_servers", "localhost:9092")
    consumer_cfg = kafka_cfg.get("consumer", {})
    topic = consumer_cfg.get("topic", "thumbnail-image-upload-stream")
    group_id = consumer_cfg.get("group_id", "wsi-thumbnail-consumer-group")

    conf = _base_config(bootstrap, broker_type)
    conf.update({
        "group.id": group_id,
        "enable.auto.commit": False,
        "auto.offset.reset": "earliest",
    })
    consumer = Consumer(conf)
    consumer.subscribe([topic])
    logger.info("Kafka consumer created (broker_type=%s, topic=%s)", broker_type, topic)
    return consumer


def create_producer():
    """Create a Kafka producer (same broker config as consumer)."""
    from confluent_kafka import Producer

    kafka_cfg = get_config()
    broker_type = kafka_cfg.get("broker_type", "plain")
    bootstrap = kafka_cfg.get("bootstrap_servers", "localhost:9092")
    producer_cfg = kafka_cfg.get("producer", {})
    topic = producer_cfg.get("topic", "processed-thumbnail-image-stream")

    conf = _base_config(bootstrap, broker_type)
    producer = Producer(conf)
    logger.info("Kafka producer created (broker_type=%s, topic=%s)", broker_type, topic)
    return producer, topic
