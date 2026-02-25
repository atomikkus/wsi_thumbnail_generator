# GCP Managed Kafka Setup for WSI Thumbnail Generator

This document describes how to provision and configure a **GCP Managed Service for Apache Kafka** cluster to drive the WSI Thumbnail Generator service asynchronously. The Kafka topics replace the direct HTTP `POST /process` call with an event-driven flow while keeping the **exact same message payloads**.

---

## Architecture Overview

```
┌─────────────────┐       ┌──────────────────────┐       ┌────────────────────────┐
│  Upstream        │       │  GCP Managed Kafka   │       │  WSI Thumbnail         │
│  Producer        │──────▶│  Topic:              │──────▶│  Generator Consumer    │
│  (e.g. LIMS/     │       │  wsi-thumbnail-req   │       │  (Cloud Run / GKE)     │
│   Pathology App) │       └──────────────────────┘       └──────────┬─────────────┘
                                                                      │
                                                                      │ Writes PNG to GCS
                                                                      ▼
                                                           ┌──────────────────────┐
                                                           │  GCP Managed Kafka   │
                                                           │  Topic:              │
                                                           │  wsi-thumbnail-resp  │
                                                           └──────────────────────┘
```

**Flow:**
1. A producer publishes a `ThumbnailRequest` JSON message to the `wsi-thumbnail-req` topic.
2. The WSI Thumbnail Generator consumer reads the message, processes the slide, uploads the PNG to GCS.
3. The consumer publishes a `ThumbnailResponse` JSON message to the `wsi-thumbnail-resp` topic.
4. Downstream consumers (viewers, dashboards, etc.) subscribe to `wsi-thumbnail-resp`.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| GCP Project with billing enabled | Replace `YOUR_PROJECT_ID` throughout this doc |
| `gcloud` CLI ≥ 501.0.0 | `gcloud components update` |
| Managed Kafka API enabled | Enabled in step 1 below |
| Service account for the consumer | Needs Kafka access + GCS write |
| Python 3.11+ (for client examples) | Matches the existing service |

---

## Step 1 — Enable the Managed Kafka API

```bash
gcloud services enable managedkafka.googleapis.com \
    --project=YOUR_PROJECT_ID
```

---

## Step 2 — Create a Managed Kafka Cluster

```bash
gcloud managed-kafka clusters create wsi-kafka-cluster \
    --project=YOUR_PROJECT_ID \
    --location=us-central1 \
    --subnets=projects/YOUR_PROJECT_ID/regions/us-central1/subnetworks/default \
    --cpu=3 \
    --memory=3221225472
```

**Parameter notes:**

| Parameter | Value | Reason |
|---|---|---|
| `--location` | `us-central1` | Match the Cloud Run region |
| `--cpu` | `3` | Minimum for a production cluster (3 vCPU = 3 brokers) |
| `--memory` | `3221225472` | 3 GiB — minimum allowed (1 GiB per vCPU) |
| `--subnets` | default VPC subnet | Use a private subnet in production |

Wait for the cluster to be `ACTIVE` (~3–5 minutes):

```bash
gcloud managed-kafka clusters describe wsi-kafka-cluster \
    --project=YOUR_PROJECT_ID \
    --location=us-central1
```

---

## Step 3 — Create Kafka Topics

### Request topic (inbound jobs)

```bash
gcloud managed-kafka topics create wsi-thumbnail-req \
    --cluster=wsi-kafka-cluster \
    --location=us-central1 \
    --project=YOUR_PROJECT_ID \
    --partitions=10 \
    --replication-factor=3
```

### Response topic (outbound results)

```bash
gcloud managed-kafka topics create wsi-thumbnail-resp \
    --cluster=wsi-kafka-cluster \
    --location=us-central1 \
    --project=YOUR_PROJECT_ID \
    --partitions=10 \
    --replication-factor=3
```

**Design notes:**
- **Partitions: 10** — allows up to 10 parallel consumer instances processing slides concurrently.
- **Replication factor: 3** — matches the 3-broker cluster for fault tolerance.
- Message key: use `slide_id` so that messages for the same slide always land in the same partition and arrive in order.

---

## Step 4 — IAM Permissions

Grant the consumer's service account (`wsi-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com`) access to the cluster:

```bash
# Kafka read/write access
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
    --member="serviceAccount:wsi-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
    --role="roles/managedkafka.client"

# GCS write access for saving thumbnails
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
    --member="serviceAccount:wsi-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
    --role="roles/storage.objectCreator"
```

> **Cloud Run note:** Assign `wsi-sa` as the Cloud Run service identity so credentials are injected automatically via the metadata server.

---

## Step 5 — Get the Bootstrap Server Address

```bash
gcloud managed-kafka clusters describe wsi-kafka-cluster \
    --project=YOUR_PROJECT_ID \
    --location=us-central1 \
    --format="value(bootstrapAddress)"
```

The output will look like:

```
bootstrap.us-central1.managedkafka.YOUR_PROJECT_ID.cloud.goog:9092
```

Use this value everywhere `KAFKA_BOOTSTRAP_SERVER` appears below.

---

## Message Schemas

The message payloads are **identical** to the existing HTTP API models in `main.py`. Encode them as **UTF-8 JSON**.

### ThumbnailRequest — published to `wsi-thumbnail-req`

```json
{
  "id": "abc123",
  "created_on": "2026-02-25T10:00:00Z",
  "image_bucket_link": "gs://my-wsi-bucket/slides/slide_001.svs",
  "patient_id": "patient-456",
  "slide_id": "slide-001",
  "block_id": "block-A"
}
```

| Field | Type | Description |
|---|---|---|
| `id` | string | Unique request identifier |
| `created_on` | string (ISO 8601) | Timestamp when the job was created |
| `image_bucket_link` | string | `gs://` or `https://storage.googleapis.com/` URL of the `.svs` / `.tif` file |
| `patient_id` | string | Patient identifier |
| `slide_id` | string | Slide identifier — **use as the Kafka message key** |
| `block_id` | string | Block identifier |

### ThumbnailResponse — published to `wsi-thumbnail-resp`

```json
{
  "id": "abc123",
  "thumbnail_image_link": "gs://my-thumbnails-bucket/slide_001_thumbnail.png",
  "patient_id": "patient-456",
  "slide_id": "slide-001",
  "metadata": {
    "width": 45000,
    "height": 38000,
    "mpp": 0.25,
    "objective_power": 40.0,
    "vendor": "aperio"
  },
  "block_id": "block-A"
}
```

| Field | Type | Description |
|---|---|---|
| `id` | string | Echoes the request `id` |
| `thumbnail_image_link` | string | `gs://` path of the generated PNG thumbnail |
| `patient_id` | string | Echoed from request |
| `slide_id` | string | Echoed from request |
| `metadata.width` | integer \| null | Slide width in pixels |
| `metadata.height` | integer \| null | Slide height in pixels |
| `metadata.mpp` | float \| null | Microns per pixel |
| `metadata.objective_power` | float \| null | Scanner magnification (e.g., 40.0) |
| `metadata.vendor` | string | Scanner vendor (e.g., `"aperio"`, `"unknown"`) |
| `block_id` | string | Echoed from request |

---

## Step 6 — Python Client Examples

Install the Kafka client:

```bash
pip install confluent-kafka
```

### Producer (publish a request)

```python
import json
from confluent_kafka import Producer

BOOTSTRAP_SERVER = "bootstrap.us-central1.managedkafka.YOUR_PROJECT_ID.cloud.goog:9092"
REQUEST_TOPIC = "wsi-thumbnail-req"

conf = {
    "bootstrap.servers": BOOTSTRAP_SERVER,
    "security.protocol": "SASL_SSL",
    "sasl.mechanism": "OAUTHBEARER",
    "sasl.oauthbearer.method": "OIDC",
    # Uses Application Default Credentials automatically on GCP
}

producer = Producer(conf)

message = {
    "id": "abc123",
    "created_on": "2026-02-25T10:00:00Z",
    "image_bucket_link": "gs://my-wsi-bucket/slides/slide_001.svs",
    "patient_id": "patient-456",
    "slide_id": "slide-001",
    "block_id": "block-A",
}

producer.produce(
    topic=REQUEST_TOPIC,
    key=message["slide_id"],          # Route by slide_id for ordering
    value=json.dumps(message).encode("utf-8"),
)
producer.flush()
print(f"Published request for slide {message['slide_id']}")
```

### Consumer (process requests, publish responses)

```python
import json
import os
from confluent_kafka import Consumer, Producer

BOOTSTRAP_SERVER = "bootstrap.us-central1.managedkafka.YOUR_PROJECT_ID.cloud.goog:9092"
REQUEST_TOPIC = "wsi-thumbnail-req"
RESPONSE_TOPIC = "wsi-thumbnail-resp"

conf = {
    "bootstrap.servers": BOOTSTRAP_SERVER,
    "group.id": "wsi-thumbnail-processor",
    "auto.offset.reset": "earliest",
    "enable.auto.commit": False,       # Manual commit after successful processing
    "security.protocol": "SASL_SSL",
    "sasl.mechanism": "OAUTHBEARER",
    "sasl.oauthbearer.method": "OIDC",
}

consumer = Consumer(conf)
producer = Producer({"bootstrap.servers": BOOTSTRAP_SERVER, **{k: v for k, v in conf.items() if k != "group.id"}})

consumer.subscribe([REQUEST_TOPIC])

while True:
    msg = consumer.poll(timeout=1.0)
    if msg is None:
        continue
    if msg.error():
        print(f"Consumer error: {msg.error()}")
        continue

    request = json.loads(msg.value().decode("utf-8"))
    print(f"Received job: {request['id']} — slide: {request['slide_id']}")

    # Call the existing HTTP service or inline processing logic
    import requests
    resp = requests.post(
        "https://YOUR_CLOUD_RUN_URL/process",
        json=request,
        timeout=120,
    )
    resp.raise_for_status()
    response = resp.json()

    producer.produce(
        topic=RESPONSE_TOPIC,
        key=response["slide_id"],
        value=json.dumps(response).encode("utf-8"),
    )
    producer.flush()

    consumer.commit(message=msg)
    print(f"Published response for slide {response['slide_id']}")
```

> **Tip:** In production, run the consumer as a separate Cloud Run Job or GKE Deployment rather than embedding the HTTP call. The consumer can call the `/process` endpoint of the existing Cloud Run service or be packaged with the processing logic directly.

---

## Step 7 — Environment Variables

Add these to your Cloud Run service or GKE deployment:

| Variable | Example Value | Purpose |
|---|---|---|
| `KAFKA_BOOTSTRAP_SERVER` | `bootstrap.us-central1.managedkafka.YOUR_PROJECT_ID.cloud.goog:9092` | Kafka broker address |
| `KAFKA_REQUEST_TOPIC` | `wsi-thumbnail-req` | Input topic name |
| `KAFKA_RESPONSE_TOPIC` | `wsi-thumbnail-resp` | Output topic name |
| `KAFKA_CONSUMER_GROUP` | `wsi-thumbnail-processor` | Consumer group ID |
| `THUMBNAIL_OUTPUT_BUCKET` | `my-thumbnails-bucket` | GCS bucket for output PNGs (existing var) |

---

## Step 8 — Deploy Consumer as Cloud Run Job

```bash
gcloud run jobs create wsi-thumbnail-consumer \
    --image=gcr.io/YOUR_PROJECT_ID/wsi-thumbnail-generator \
    --region=us-central1 \
    --service-account=wsi-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com \
    --set-env-vars="KAFKA_BOOTSTRAP_SERVER=bootstrap.us-central1.managedkafka.YOUR_PROJECT_ID.cloud.goog:9092" \
    --set-env-vars="KAFKA_REQUEST_TOPIC=wsi-thumbnail-req" \
    --set-env-vars="KAFKA_RESPONSE_TOPIC=wsi-thumbnail-resp" \
    --set-env-vars="THUMBNAIL_OUTPUT_BUCKET=my-thumbnails-bucket" \
    --max-retries=3 \
    --parallelism=5
```

---

## Topic Retention and Storage

Configure message retention to prevent unbounded GCS-like storage costs:

```bash
# Keep request messages for 7 days
gcloud managed-kafka topics update wsi-thumbnail-req \
    --cluster=wsi-kafka-cluster \
    --location=us-central1 \
    --project=YOUR_PROJECT_ID \
    --configs=retention.ms=604800000

# Keep response messages for 30 days for audit / replay
gcloud managed-kafka topics update wsi-thumbnail-resp \
    --cluster=wsi-kafka-cluster \
    --location=us-central1 \
    --project=YOUR_PROJECT_ID \
    --configs=retention.ms=2592000000
```

---

## Monitoring

GCP Managed Kafka exposes metrics automatically in **Cloud Monitoring**. Useful metrics:

| Metric | What to Watch |
|---|---|
| `managedkafka.googleapis.com/consumer_group/lag` | Consumer falling behind — scale up consumers |
| `managedkafka.googleapis.com/topic/message_count` | Volume of incoming jobs |
| `managedkafka.googleapis.com/cluster/cpu_utilization` | Cluster capacity |

Create an alert for consumer lag exceeding 500 messages:

```bash
gcloud alpha monitoring policies create \
    --notification-channels=YOUR_CHANNEL_ID \
    --display-name="WSI Kafka Consumer Lag Alert" \
    --condition-display-name="Consumer lag > 500" \
    --condition-filter='resource.type="managedkafka.googleapis.com/ConsumerGroup" AND metric.type="managedkafka.googleapis.com/consumer_group/lag"' \
    --condition-threshold-value=500 \
    --condition-threshold-comparison=COMPARISON_GT \
    --condition-aggregations-alignment-period=60s \
    --condition-aggregations-per-series-aligner=ALIGN_MAX
```

---

## Quick Reference

```bash
# List clusters
gcloud managed-kafka clusters list --location=us-central1 --project=YOUR_PROJECT_ID

# List topics
gcloud managed-kafka topics list --cluster=wsi-kafka-cluster --location=us-central1 --project=YOUR_PROJECT_ID

# Describe a topic
gcloud managed-kafka topics describe wsi-thumbnail-req --cluster=wsi-kafka-cluster --location=us-central1 --project=YOUR_PROJECT_ID

# Delete a cluster (destructive — removes all topics and data)
gcloud managed-kafka clusters delete wsi-kafka-cluster --location=us-central1 --project=YOUR_PROJECT_ID
```
