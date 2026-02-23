from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import fsspec
import tifffile
import io
import os
from PIL import Image
import logging
import re

# Configure basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="WSI Thumbnail Generator", 
    description="Fast, minimal Thumbnail Generator for WSI (.svs) using HTTP Range Requests. Supports GCS public and signed URLs."
)

# Enable CORS (useful if calling from a frontend like React/OpenSeadragon)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*", "OPTIONS"],
    allow_headers=["*"],
)

class ThumbnailRequest(BaseModel):
    id: str
    created_on: str
    image_bucket_link: str
    patient_id: str
    slide_id: str
    block_id: str

class ThumbnailResponse(BaseModel):
    id: str
    thumbnail_image_link: str
    patient_id: str
    slide_id: str
    metadata: dict
    block_id: str

@app.get("/health")
def health_check():
    """Simple health check endpoint for Cloud Run."""
    return {"status": "ok"}

import re

@app.get("/metadata")
def get_metadata(url: str = Query(..., description="Public or Signed HTTP/HTTPS URL of the WSI file (.svs, .tif)")):
    """
    Extracts physical dimensions and Microns Per Pixel (MPP) from the WSI file. 
    Crucial for Viewer tools like measurement (ruler) annotations.
    """
    logger.info(f"Extracting metadata for URL: {url[:60]}...")
    try:
        with fsspec.open(url, "rb", block_size=1 * 1024 * 1024) as f:
            with tifffile.TiffFile(f) as tif:
                
                # Get the main full-resolution page
                page = tif.pages[0]
                
                width = page.shape[1]
                height = page.shape[0]
                
                mpp = None
                vendor = "unknown"
                objective = None
                
                # SVS files store extensive metadata in the ImageDescription tag
                if 'ImageDescription' in page.tags:
                    desc = page.tags['ImageDescription'].value
                    if isinstance(desc, str):
                        # Try to parse Aperio specifically
                        if 'Aperio' in desc or 'aperio' in desc.lower():
                            vendor = "aperio"
                            # SVS format usually has MPP explicitly defined like "MPP = 0.25"
                            mpp_match = re.search(r'MPP\s*=\s*([0-9.]+)', desc)
                            if mpp_match:
                                mpp = float(mpp_match.group(1))
                            
                            obj_match = re.search(r'AppMag\s*=\s*([0-9.]+)', desc)
                            if obj_match:
                                objective = float(obj_match.group(1))
                
                # Fallback to standard TIFF tags if Aperio description string isn't found
                if mpp is None:
                    # resolution is usually a tuple (numerator, denominator)
                    if 'XResolution' in page.tags and 'ResolutionUnit' in page.tags:
                        x_res_tag = page.tags['XResolution'].value
                        res_unit = page.tags['ResolutionUnit'].value
                        
                        try:
                            # Typically x_res_tag is like (100000, 1) or just 100000
                            if isinstance(x_res_tag, tuple):
                                x_res = x_res_tag[0] / x_res_tag[1]
                            else:
                                x_res = float(x_res_tag)
                            
                            # cm (3) is most common. (10000 micrometers in 1 cm)
                            if res_unit == 3 and x_res > 0:
                                mpp = 10000.0 / x_res
                        except (TypeError, ZeroDivisionError) as e:
                            logger.warning(f"Could not calculate MPP from standard TIFF tags: {e}")
                
                return {
                    "width": width,
                    "height": height,
                    "mpp": mpp,
                    "objective_power": objective,
                    "vendor": vendor,
                    "source_url": url
                }
                
    except Exception as e:
        logger.error(f"Error handling metadata request: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/thumbnail", response_class=Response)
def get_thumbnail(
    url: str = Query(..., description="Public or Signed HTTP/HTTPS URL of the WSI file (.svs, .tif)"),
    max_size: int = Query(512, description="Maximum width/height of the generated thumbnail")
):
    """
    Given a remote HTTP URL for a Whole Slide Image, intelligently extract the thumbnail
    without downloading the entire multigigabyte file. It uses fsspec HTTP Range requests 
    and tifffile to read only the metadata and the thumbnail page.
    """
    logger.info(f"Generating thumbnail for URL: {url[:60]}... (max_size={max_size})")
    
    try:
        # Use fsspec to open the remote URL stream. 
        # block_size dictates the chunk sizes of HTTP byte ranges requested.
        with fsspec.open(url, "rb", block_size=1 * 1024 * 1024) as f:
            with tifffile.TiffFile(f) as tif:
                img_array = None
                
                logger.info("Loaded TIFF metadata. Extracting thumbnail from the main slide pyramid.")
                try:
                    # tifffile parses standard pyramidal images into 'series'.
                    # SVS files main image is typically in series 0.
                    # We extract the smallest level of the pyramid to represent the full slide.
                    pyramid = tif.series[0]
                    smallest_level = pyramid.levels[-1]
                    logger.info(f"Using smallest pyramid level with shape {smallest_level.shape} representing the actual slide.")
                    img_array = smallest_level.asarray()
                except (IndexError, AttributeError) as series_e:
                    logger.warning(f"Could not read from pyramid series: {series_e}")
                    num_pages = len(tif.pages)
                    if num_pages > 0:
                        # Last resort: just use the first page (DANGEROUS if massive, but shouldn't happen with valid WSI)
                        logger.info("Falling back to reading Page 0.")
                        img_array = tif.pages[0].asarray()
                    else:
                        raise ValueError("TIFF file contains no valid imaging pages.")
                
                # Process the numpy array into a PIL Image
                # We do this to ensure common RGB mode and standard resizing.
                img = Image.fromarray(img_array)
                
                # Resize keeping aspect ratio
                img.thumbnail((max_size, max_size))
                
                # Encode as PNG bytes in memory
                out = io.BytesIO()
                img.save(out, format="PNG")
                img_bytes = out.getvalue()
                
                logger.info(f"Successfully generated HTTP thumbnail ({len(img_bytes)} bytes).")
                
                return Response(
                    content=img_bytes, 
                    media_type="image/png", 
                    headers={"Cache-Control": "public, max-age=86400"}
                )

    except Exception as e:
        logger.error(f"Error handling thumbnail request: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/process", response_model=ThumbnailResponse)
def process_wsi(request: ThumbnailRequest):
    """
    POST endpoint to process WSI: extracts metadata, generates a PNG thumbnail,
    saves it back to GCS, and returns the requested JSON payload.
    """
    logger.info(f"Processing request for ID: {request.id}, URL: {request.image_bucket_link}")
    
    metadata = {
        "width": None,
        "height": None,
        "mpp": None,
        "objective_power": None,
        "vendor": "unknown"
    }
    
    try:
        # Open source image via fsspec
        with fsspec.open(request.image_bucket_link, "rb", block_size=1 * 1024 * 1024) as f:
            with tifffile.TiffFile(f) as tif:
                
                # --- 1. EXTRACT METADATA ---
                if len(tif.pages) > 0:
                    page = tif.pages[0]
                    metadata["width"] = page.shape[1]
                    metadata["height"] = page.shape[0]
                    
                    if 'ImageDescription' in page.tags:
                        desc = page.tags['ImageDescription'].value
                        if isinstance(desc, str):
                            if 'Aperio' in desc or 'aperio' in desc.lower():
                                metadata["vendor"] = "aperio"
                                mpp_match = re.search(r'MPP\s*=\s*([0-9.]+)', desc)
                                if mpp_match:
                                    metadata["mpp"] = float(mpp_match.group(1))
                                obj_match = re.search(r'AppMag\s*=\s*([0-9.]+)', desc)
                                if obj_match:
                                    metadata["objective_power"] = float(obj_match.group(1))
                    
                    if metadata["mpp"] is None and 'XResolution' in page.tags and 'ResolutionUnit' in page.tags:
                        x_res_tag = page.tags['XResolution'].value
                        res_unit = page.tags['ResolutionUnit'].value
                        try:
                            x_res = x_res_tag[0] / x_res_tag[1] if isinstance(x_res_tag, tuple) else float(x_res_tag)
                            if res_unit == 3 and x_res > 0:
                                metadata["mpp"] = 10000.0 / x_res
                        except:
                            pass
                
                # --- 2. EXTRACT THUMBNAIL ARRAY ---
                img_array = None
                try:
                    pyramid = tif.series[0]
                    smallest_level = pyramid.levels[-1]
                    img_array = smallest_level.asarray()
                except:
                    if len(tif.pages) > 0:
                        img_array = tif.pages[0].asarray()
                    else:
                        raise ValueError("TIFF contains no image pages.")

        # --- 3. PROCESS THE THUMBNAIL (PNG) ---
        img = Image.fromarray(img_array)
        img.thumbnail((512, 512)) # arbitrary max size, keeping aspect ratio
        out = io.BytesIO()
        img.save(out, format="PNG")
        img_bytes = out.getvalue()
        
        # --- 4. UPLOAD BACK TO GCS ---
        # Derive output link. If the input is https://storage.googleapis.com/bucket/path, 
        # we MUST convert it to gs://bucket/path because fsspec cannot write via https:// (read-only)
        
        base_path, _ = os.path.splitext(request.image_bucket_link)
        parsed_path = base_path
        if parsed_path.startswith("https://storage.googleapis.com/"):
            parsed_path = parsed_path.replace("https://storage.googleapis.com/", "gs://")
        elif "storage.cloud.google.com/" in parsed_path:
            parsed_path = re.sub(r'https?://storage\.cloud\.google\.com/', 'gs://', parsed_path)
            
        thumbnail_bucket_link = f"{parsed_path}_thumbnail.png"
        
        # Override destination bucket and path if THUMBNAIL_OUTPUT_BUCKET is provided as an env var!
        # Example output bucket name: my-thumbnails-bucket
        output_bucket = os.environ.get("THUMBNAIL_OUTPUT_BUCKET", "")
        if output_bucket:
            # Reconstruct the GS path to write the file strictly to the specified bucket
            # while maintaining the original filename
            filename = os.path.basename(thumbnail_bucket_link)
            thumbnail_bucket_link = f"gs://{output_bucket}/{filename}"
            
        logger.info(f"Saving generated thumbnail to {thumbnail_bucket_link}")
        
        # Write to GCS. gcsfs handles authentication natively using Cloud Run Application Default Credentials.
        # Ensure your Cloud Run service identity holds 'Storage Object Creator' on the target bucket.
        with fsspec.open(thumbnail_bucket_link, "wb", token="google_default") as out_f:
            out_f.write(img_bytes)
            
        return ThumbnailResponse(
            id=request.id,
            thumbnail_image_link=thumbnail_bucket_link,
            patient_id=request.patient_id,
            slide_id=request.slide_id,
            metadata=metadata,
            block_id=request.block_id
        )

    except Exception as e:
        logger.error(f"Error processing POST request: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
