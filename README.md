# WSI Thumbnail Generator

This service reads Multi-Page TIFF and Aperio `.svs` files hosted dynamically on Google Cloud Storage (GCS) using HTTP Range Requests. Rather than downloading gigabytes of imaging data, it smartly parses the TIFF metadata to stream just the required subset of bytes corresponding to the thumbnail page.

## Features
- **Ultra-fast:** Processes thumbnails in under a second.
- **Efficient Memory and Network usage:** Zero full-file downloads, using HTTP partial content features.
- **Dynamic Secure Access:** Supports public HTTP/HTTPS URLs as well as GCS Signed URLs securely.
- **Serverless Ready:** Configured to deploy out-of-the-box on Google Cloud Run.

## Local Development Setup

1. **Install dependencies:**
   ```bash
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Run the FastAPI server locally:**
   ```bash
   uvicorn main:app --reload --host 0.0.0.0 --port 8080
   ```

3. **Test the endpoint:**
   Navigate your browser to:
   ```
   http://localhost:8080/thumbnail?url=YOUR_SVS_PUBLIC_OR_SIGNED_URL
   ```

## Deploying on Google Cloud Run (GCP), eg.

This API is stateless, containerized, and perfectly suited for **Google Cloud Run**.

1. Submit a build to Google Cloud Build (or build locally):
   ```bash
   gcloud builds submit --tag gcr.io/YOUR_PROJECT_ID/wsi-thumbnail-generator
   ```

2. Deploy to Cloud Run:
   ```bash
   gcloud run deploy wsi-thumbnail-generator \
       --image gcr.io/YOUR_PROJECT_ID/wsi-thumbnail-generator \
       --platform managed \
       --region us-central1 \
       --allow-unauthenticated \
       --memory 1Gi \
       --cpu 1 
   ```

*(If you are deploying securely, omit `--allow-unauthenticated` and configure IAM permissions for service-to-service communication).*

## API Authentication (Basic Auth)

The API supports optional **HTTP Basic Auth** to protect `/metadata`, `/thumbnail`, and `/process`. The `/health` endpoint is always unauthenticated for load balancers and health checks.

- **Enable auth:** Set environment variables **`API_USERNAME`** and **`API_PASSWORD`**. If either is missing or empty, auth is disabled and the endpoints remain open.
- **Cloud Run:** In the Cloud Run console, go to your service → **Edit & deploy new revision** → **Variables & secrets** and add `API_USERNAME` and `API_PASSWORD` (or use Secret Manager for the password).
- **Local:** Export before starting the server, e.g. `export API_USERNAME=myuser API_PASSWORD=mysecret` (or set in your shell/config).

**Calling the API with auth (e.g. curl):**
```bash
curl -u "USERNAME:PASSWORD" "https://YOUR_SERVICE_URL/metadata?url=YOUR_WSI_URL"
curl -u "USERNAME:PASSWORD" "https://YOUR_SERVICE_URL/thumbnail?url=YOUR_WSI_URL"
```

## Architectural Note
`tifffile` and `fsspec` together give us the ability to seek through HTTP files just like a local mounted file system. `imagecodecs` ensures we can rapidly decompress standard WSI codecs like JPEG 2000.

## Security / Auth (Writing to GCS)

When the `/process` POST endpoint writes thumbnails back to Google Cloud Storage (e.g., `gs://bucket-name/...`), it relies on the `gcsfs` library. 

**Cloud Run**: If deploying to Cloud Run, simply assure the Service Account assigned to the Cloud Run instance has the **Storage Object Admin** or **Storage Object Creator** role on the specific GCP bucket. Authentication will happen automatically!

