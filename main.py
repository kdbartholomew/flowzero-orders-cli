import sys
import os
import json
import re
import io
import mimetypes
import uuid
import time
from pathlib import Path
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from collections import defaultdict
from typing import List, Tuple

import click
import requests
import boto3
import geopandas as gpd
from dotenv import load_dotenv
from rich.console import Console
from shapely.geometry import shape

from generate_aoi import start_aoi_server

# Load environment variables
load_dotenv()

console = Console()
ORDERS_LOG_FILE = Path("orders.json")
API_URL = "https://api.planet.com/basemaps/v1/mosaics"
MIN_COV_PCT = 98.0

# Initialize S3 client
s3 = boto3.client(
    's3',
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY")
)
S3_BUCKET = "flowzero"

# --- Utility Functions ---

def normalize_aoi_name(raw_name: str) -> str:
    '''Normalize AOI name by removing prefixes and suffixes.'''
    cleaned = re.sub(r"^(DrySpy_)?AOI_", "", raw_name)
    cleaned = re.sub(r"_(central|north|south|east|west)$", "", cleaned, flags=re.IGNORECASE)
    return cleaned

def log_order(order_data):
    """Append an order log entry with metadata to orders.json."""
    entry = order_data.copy()
    entry["timestamp"] = datetime.now().isoformat()
    if ORDERS_LOG_FILE.exists():
        try:
            with ORDERS_LOG_FILE.open("r") as f:
                orders = json.load(f)
        except json.JSONDecodeError:
            orders = []
    else:
        orders = []
    orders.append(entry)
    with ORDERS_LOG_FILE.open("w") as f:
        json.dump(orders, f, indent=2)


def s3_key_exists(bucket: str, key: str) -> bool:
    """Check if a key exists in S3."""
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except:
        return False


def fetch_all_search_results(search_url: str, search_payload: dict, api_key: str, search_headers: dict) -> List[dict]:
    """
    Fetch all search results from Planet API, handling pagination.
    
    Follows Planet API pagination pattern: check _links["_next"] until it's gone.
    Returns a list of all features from all pages.
    """
    all_features = []
    current_url = search_url
    current_payload = search_payload
    is_first_request = True
    
    while True:
        if is_first_request:
            # First request uses POST with payload
            response = requests.post(current_url, json=current_payload, auth=(api_key, ""), headers=search_headers)
            is_first_request = False
        else:
            # Subsequent pagination requests use GET
            response = requests.get(current_url, auth=(api_key, ""), headers=search_headers)
        
        if response.status_code != 200:
            raise Exception(f"Search failed: {response.status_code} - {response.text}")
        
        data = response.json()
        features = data.get("features", [])
        all_features.extend(features)
        
        # Follow pagination - Planet API uses "_next" (with underscore) in _links
        if "_links" in data and "_next" in data["_links"]:
            current_url = data["_links"]["_next"]
            # Small delay to be respectful to API
            time.sleep(0.5)
        else:
            # No more pages
            break
    
    return all_features

def extract_date_from_filename(filename):
    """Extract the acquisition date from Planet product filename."""
    pattern = r"(\d{4})(\d{2})(\d{2})_"
    match = re.search(pattern, filename)
    if match:
        year, month, day = match.groups()
        return f"{year}_{month}_{day}"
    return None

def extract_scene_id(filename):
    """Extract scene ID from Planet product filename."""
    pattern = r"\d{8}_(\w+)_"
    match = re.search(pattern, filename)
    if match:
        return match.group(1)
    return None

def get_week_start_date(date_str):
    """Get start of week (Sunday) for a given date string (YYYY_MM_DD)."""
    year, month, day = map(int, date_str.split('_'))
    date_obj = datetime(year, month, day)
    days_to_sunday = date_obj.weekday() + 1
    if days_to_sunday == 7:
        return date_str
    sunday = date_obj - timedelta(days=days_to_sunday)
    return sunday.strftime('%Y_%m_%d')


def subdivide_date_range(start_date: str, end_date: str, max_months: int = 6) -> List[Tuple[str, str]]:
    """
    Subdivide a date range into chunks of max_months or less.
    
    Args:
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        max_months: Maximum number of months per chunk (default 9)
    
    Returns:
        List of (start_date, end_date) tuples for each chunk
    """
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    
    chunks = []
    current_start = start_dt
    
    while current_start <= end_dt:
        # Calculate chunk end: either max_months from start or the overall end date
        chunk_end = current_start + relativedelta(months=max_months) - timedelta(days=1)
        if chunk_end > end_dt:
            chunk_end = end_dt
        
        chunks.append((
            current_start.strftime("%Y-%m-%d"),
            chunk_end.strftime("%Y-%m-%d")
        ))
        
        # Move to next chunk
        current_start = chunk_end + timedelta(days=1)
    
    return chunks


def submit_single_order(
    aoi_geom,
    aoi_geojson: dict,
    aoi_area_sqkm: float,
    start_date: str,
    end_date: str,
    gage_id: str,
    num_bands: str,
    product_bundle: str,
    product_bundle_order: str,
    cadence: str,
    api_key: str,
    dry_run: bool = False,
    batch_id: str = None
) -> dict:
    """
    Submit a single order to Planet API.
    
    Returns dict with order info or error details.
    """
    start_date_iso = f"{start_date}T00:00:00Z"
    end_date_iso = f"{end_date}T23:59:59Z"
    
    # Perform scene search
    search_url = "https://api.planet.com/data/v1/quick-search"
    search_payload = {
        "item_types": ["PSScene"],
        "filter": {
            "type": "AndFilter",
            "config": [
                {"type": "GeometryFilter", "field_name": "geometry", "config": aoi_geojson},
                {"type": "DateRangeFilter", "field_name": "acquired", "config": {"gte": start_date_iso, "lte": end_date_iso}},
                {"type": "RangeFilter", "field_name": "cloud_cover", "config": {"lte": 0.0}},
                {"type": "AssetFilter", "config": [product_bundle]},
                {"type":"StringInFilter", "field_name":"quality_category", "config":["standard"]}
            ]
        }
    }
    
    search_headers = {"Content-Type": "application/json"}
    
    # Fetch all results with pagination
    try:
        features = fetch_all_search_results(search_url, search_payload, api_key, search_headers)
    except Exception as e:
        return {"success": False, "error": str(e)}
    
    if not features:
        return {"success": False, "error": "No cloud-free scenes found", "scenes_found": 0}
    
    def get_interval_key(date_obj):
        if cadence == "daily":
            return date_obj.strftime("%Y-%m-%d")
        elif cadence == "weekly":
            sunday = date_obj - timedelta(days=date_obj.weekday() + 1 if date_obj.weekday() != 6 else 0)
            return sunday.strftime("%Y-%m-%d")
        elif cadence == "monthly":
            return date_obj.strftime("%Y-%m")
    
    scene_groups = defaultdict(list)
    
    for feature in features:
        props = feature["properties"]
        geom = shape(feature["geometry"])
        intersect_area = geom.intersection(aoi_geom).area
        coverage_pct = (intersect_area / aoi_geom.area) * 100
        if coverage_pct < MIN_COV_PCT:
            continue
        
        date_str = props["acquired"][:10]
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        key = get_interval_key(date_obj)
        scene_groups[key].append((coverage_pct, date_obj, feature))
    
    # Sort each group by coverage descending, then date ascending; select first
    selected = []
    for group in scene_groups.values():
        group.sort(key=lambda x: (-x[0], x[1]))  # coverage desc, date asc
        coverage_pct, date, f = group[0]
        selected.append((f, coverage_pct, date))
    
    if not selected:
        return {"success": False, "error": "No full-coverage scenes matched filter", "scenes_found": len(features)}
    
    item_ids = [f["id"] for f, _, _ in selected]
    
    # Calculate quota impact (area in sq km * number of scenes = total quota used)
    quota_sqkm = aoi_area_sqkm * len(selected)
    quota_hectares = quota_sqkm * 100  # 1 sq km = 100 hectares
    
    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "gage_id": gage_id,
            "start_date": start_date,
            "end_date": end_date,
            "scenes_found": len(features),
            "scenes_selected": len(selected),
            "aoi_area_sqkm": aoi_area_sqkm,
            "quota_sqkm": quota_sqkm,
            "quota_hectares": quota_hectares,
            "item_ids": item_ids
        }
    
    # Submit order
    order_url = "https://api.planet.com/compute/ops/orders/v2"
    order_payload = {
        "name": f"PSScope Order {gage_id} {start_date} to {end_date}",
        "products": [{
            "item_ids": item_ids,
            "item_type": "PSScene",
            "product_bundle": product_bundle_order
        }],
        "tools": [
            {"clip": {"aoi": aoi_geojson}}
        ]
    }
    
    response = requests.post(order_url, json=order_payload, auth=(api_key, ""), headers=search_headers)
    
    if response.status_code == 202:
        order_id = response.json()["id"]
        order_log = {
            "order_id": order_id,
            "aoi_name": gage_id,
            "order_type": "PSScope",
            "start_date": start_date,
            "end_date": end_date,
            "num_bands": num_bands,
            "product_bundle": product_bundle,
            "product_bundle_order": product_bundle_order,
            "clipped": True,
            "aoi_area_sqkm": aoi_area_sqkm,
            "scenes_selected": len(selected),
            "batch_order": True,
            "timestamp": datetime.now().isoformat()
        }
        if batch_id:
            order_log["batch_id"] = batch_id
        log_order(order_log)
        return {
            "success": True,
            "order_id": order_id,
            "gage_id": gage_id,
            "start_date": start_date,
            "end_date": end_date,
            "scenes_found": len(features),
            "scenes_selected": len(selected),
            "aoi_area_sqkm": aoi_area_sqkm,
            "quota_sqkm": quota_sqkm,
            "quota_hectares": quota_hectares
        }
    else:
        return {"success": False, "error": f"Order failed: {response.status_code} - {response.text}"}

# --- CLI Commands ---

@click.group()
def cli():
    """FlowZero - River Monitoring Tool using Planet Satellite Data"""
    pass

@cli.command()
def generate_aoi():
    """Launch interactive AOI generation web interface."""
    console.print("🌍 Launching AOI generation server...", style="bold green")
    console.print("📝 Open your browser at http://localhost:5000", style="bold blue")
    start_aoi_server()

@cli.command()
@click.option('--shp', required=True, type=click.Path(exists=True), help='Path to input Shapefile')
@click.option("--output", default="./geojsons", help="Directory to save GeoJSONs")
def convert_shp(shp, output):
    """Convert Shapefile to GeoJSON with proper CRS handling."""
    try:
        output_dir = Path(output)
        output_dir.mkdir(parents=True, exist_ok=True)
        shp_path = Path(shp)
        geojson_path = output_dir / f"{shp_path.stem}.geojson"
        gdf = gpd.read_file(shp_path)
        if gdf.crs is None or gdf.crs.to_string() == "unknown":
            gdf.set_crs(epsg=4326, inplace=True)
        elif gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
        gdf.to_file(geojson_path, driver="GeoJSON")
        console.print(f"✅ Shapefile converted successfully: {geojson_path}", style="bold green")
    except Exception as e:
        console.print(f"❌ Error converting Shapefile: {str(e)}", style="bold red")
        sys.exit(1)

@cli.command()
@click.option("--geojson", required=True, type=click.Path(exists=True), help="Path to AOI GeoJSON")
@click.option("--start-date", required=True, help="Start date (YYYY-MM-DD)")
@click.option("--end-date", required=True, help="End date (YYYY-MM-DD)")
@click.option("--num-bands", type=click.Choice(['four_bands', 'eight_bands']), default='four_bands', help="Choose 4B or 8B imagery")
@click.option("--api-key", default=os.getenv("PL_API_KEY"), help="Planet API Key")
@click.option("--bundle", default=None, help="Override bundle name to use")
@click.option("--cadence", type=click.Choice(["daily", "weekly", "monthly"]), default="weekly", help="Scene selection cadence")
def submit(geojson, start_date, end_date, num_bands, api_key, bundle, cadence):
    """Submit a new PlanetScope imagery order (PSScope Scenes) with AOI clipping."""
    try:
        from shapely.geometry import shape
        from collections import defaultdict

        start_date_iso = f"{start_date}T00:00:00Z"
        end_date_iso = f"{end_date}T23:59:59Z"

        gdf = gpd.read_file(geojson)
        gdf = gdf.to_crs(epsg=4326)
        aoi_geom = gdf.geometry.union_all()
        aoi = aoi_geom.__geo_interface__

        gdf_equal_area = gdf.to_crs(epsg=6933)  # World Cylindrical Equal Area
        # Compute area in sq km using equal-area CRS
        aoi_area_sqkm = gdf_equal_area.area.sum() / 1e6  # m² → km²
        console.print(f"[✓] AOI area: {aoi_area_sqkm:.2f} sq km", style="bold blue")

        start_year = int(start_date.split('-')[0])

        if bundle:
            product_bundle = bundle
            console.print(f"[✅] Using override bundle: {product_bundle}", style="bold blue")
        elif num_bands == "four_bands":
            product_bundle = "ortho_analytic_4b_sr"
            console.print(f"[✅] Using 4-band surface reflectance: {product_bundle}", style="bold blue")
        else:
            product_bundle = "ortho_analytic_8b_sr" if start_year >= 2021 else "ortho_analytic_4b_sr"
            console.print(f"[✅] Using 8-band surface reflectance: {product_bundle}", style="bold blue")
        
        #Cross walk product bundle for ordering
        if product_bundle == "ortho_analytic_4b_sr":
            product_bundle_order = "analytic_sr_udm2"
        elif product_bundle == "ortho_analytic_8b_sr":
            product_bundle_order = "analytic_8b_sr_udm2"
        else:
            product_bundle_order = product_bundle
        
        # Perform scene search with cadence filtering (as in search-scenes)
        search_url = "https://api.planet.com/data/v1/quick-search"
        search_payload = {
            "item_types": ["PSScene"],
            "filter": {
                "type": "AndFilter",
                "config": [
                    {"type": "GeometryFilter", "field_name": "geometry", "config": aoi},
                    {"type": "DateRangeFilter", "field_name": "acquired", "config": {"gte": start_date_iso, "lte": end_date_iso}},
                    {"type": "RangeFilter", "field_name": "cloud_cover", "config": {"lte": 0.0}},
                    {"type": "AssetFilter", "config": [product_bundle]},
                    {"type":"StringInFilter", "field_name":"quality_category", "config":["standard"]}
                ]
            }
        }

        search_headers = {"Content-Type": "application/json"}
        
        # Fetch all results with pagination
        try:
            features = fetch_all_search_results(search_url, search_payload, api_key, search_headers)
        except Exception as e:
            console.print(f"❌ Failed to search for scenes: {str(e)}", style="bold red")
            return

        if not features:
            console.print("[yellow]No cloud-free PlanetScope scenes found.[/yellow]")
            return

        def get_interval_key(date_obj):
            if cadence == "daily":
                return date_obj.strftime("%Y-%m-%d")
            elif cadence == "weekly":
                sunday = date_obj - timedelta(days=date_obj.weekday() + 1 if date_obj.weekday() != 6 else 0)
                return sunday.strftime("%Y-%m-%d")
            elif cadence == "monthly":
                return date_obj.strftime("%Y-%m")

        scene_groups = defaultdict(list)

        for feature in features:
            props = feature["properties"]
            geom = shape(feature["geometry"])
            intersect_area = geom.intersection(aoi_geom).area
            coverage_pct = (intersect_area / aoi_geom.area) * 100
            if coverage_pct < MIN_COV_PCT:
                continue

            date_str = props["acquired"][:10]
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
            key = get_interval_key(date_obj)
            scene_groups[key].append((coverage_pct, date_obj, feature))

        # Sort each group by coverage descending, then date ascending; select first
        selected = []
        for group in scene_groups.values():
            group.sort(key=lambda x: (-x[0], x[1]))  # coverage desc, date asc
            coverage_pct, date, f = group[0]
            selected.append((f, coverage_pct, date))

        if not selected:
            console.print("[yellow]No full-coverage scenes matched filter.[/yellow]")
            return

        console.print(f"[green]Selected {len(selected)} best scenes ({cadence})[/green]")
        for f, cov, dt in selected:
            thumb = f["_links"].get("thumbnail")
            console.print(f"{dt.date()} | ID: {f['id']} | Coverage: {cov:.2f}% | [link={thumb}]thumbnail[/link]")

        # Print the total sqkm covered by this order and ask the user if they want to proceed
        console.print(f"[blue]Total quota used in this order: {aoi_area_sqkm * len(selected):.2f} sq km[/blue]")
        proceed = console.input("[yellow]Proceed with order? (y/n): [/yellow]")
        if proceed.lower() != "y":
            console.print("[red]Order cancelled by user.[/red]")
            return

        item_ids = [f["id"] for f, _, _ in selected]

        order_url = "https://api.planet.com/compute/ops/orders/v2"
        order_payload = {
            "name": f"PSScope Order {Path(geojson).stem}",
            "products": [{
                "item_ids": item_ids,
                "item_type": "PSScene",
                "product_bundle": product_bundle_order
            }],
            "tools": [
                {"clip": {"aoi": aoi}}
            ]
        }

        response = requests.post(order_url, json=order_payload, auth=(api_key, ""), headers=search_headers)

        if response.status_code == 202:
            order_id = response.json()["id"]
            console.print(f"✅ Order submitted successfully! Order ID: {order_id}", style="bold green")
            log_order({
                "order_id": order_id,
                "aoi_name": normalize_aoi_name(Path(geojson).stem),
                "order_type": "PSScope",
                "start_date": start_date,
                "end_date": end_date,
                "num_bands": num_bands,
                "product_bundle": product_bundle_order,
                "clipped": True,
                "aoi_area_sqkm": aoi_area_sqkm,
                "timestamp": datetime.now().isoformat()
            })
        else:
            console.print(f"❌ Order submission failed: {response.status_code} - {response.text}...", style="bold red")

    except Exception as e:
        console.print(f"❌ Error: {str(e)}", style="bold red")
        sys.exit(1)


@cli.command()
@click.option("--mosaic-name", required=True, help="Mosaic name from list_basemaps")
@click.option("--geojson", type=click.Path(exists=True), help="Path to AOI GeoJSON")
@click.option("--api-key", default=os.getenv("PL_API_KEY"), help="Planet API Key")
def order_basemap(mosaic_name, geojson, api_key):
    """Order a Basemap using a given Mosaic name and AOI."""
    if not api_key:
        console.print("[red]Error: API key is missing.[/red]")
        return

    if geojson:
        gdf = gpd.read_file(geojson)
        aoi = gdf.geometry.unary_union.__geo_interface__
    else:
        console.print("[red]Error: A GeoJSON file must be provided.[/red]")
        return

    order_payload = {
        "name": f"Basemap Order {mosaic_name}",
        "source_type": "basemaps",
        "products": [{"mosaic_name": mosaic_name, "geometry": aoi}],
        "tools": [{"clip": {}}]
    }

    response = requests.post("https://api.planet.com/compute/ops/orders/v2", json=order_payload, auth=(api_key, ""))
    if response.status_code == 202:
        order_info = response.json()
        console.print(f"✅ Order submitted successfully! Order ID: {order_info['id']}", style="bold green")
        log_order({
            "order_id": order_info['id'],
            "order_type": "Basemap (Composite)",
            "aoi_name": normalize_aoi_name(Path(geojson).stem),
            "mosaic_name": mosaic_name,
            "start_date": "N/A",
            "end_date": "N/A",
            "timestamp": datetime.now().isoformat()
        })

    else:
        console.print(f"[red]Error submitting order: {response.text}[/red]")

@cli.command()
@click.argument("order_id")
@click.option("--api-key", default=os.getenv("PL_API_KEY"), help="Planet API Key")
def check_order_status(order_id, api_key):
    """Check order status and upload to S3 if completed."""
    response = requests.get(f"https://api.planet.com/compute/ops/orders/v2/{order_id}", auth=(api_key, ""))

    if response.status_code != 200:
        console.print(f"[❌] Error checking order status: {response.text}", style="bold red")
        return

    order_info = response.json()
    order_state = order_info["state"]
    console.print(f"[✅] Order Status: {order_state}")

    if order_state != "success":
        return

    aoi_name = "UnknownAOI"
    mosaic_name = "UnknownMosaic"
    order_type = "Unknown"
    num_bands = "four_bands"
    product_bundle = None

    if ORDERS_LOG_FILE.exists():
        with open(ORDERS_LOG_FILE, "r") as f:
            try:
                orders = json.load(f)
                match = next((o for o in orders if o["order_id"] == order_id), {})
                aoi_name_raw = match.get("aoi_name", "UnknownAOI")
                aoi_name = normalize_aoi_name(aoi_name_raw)
                mosaic_name = match.get("mosaic_name", "unknown_mosaic")
                order_type = match.get("order_type", "Unknown")
                num_bands = match.get("num_bands", "four_bands")
                product_bundle = match.get("product_bundle")
                console.print(f"[✅] Found order metadata: AOI={aoi_name}, Type={order_type}, Bundle={product_bundle}", style="bold green")
            except Exception as e:
                console.print(f"[yellow]⚠️ Could not read orders.json: {e}[/yellow]")

    is_basemap = "source_type" in order_info and order_info["source_type"] == "basemaps"

    download_links = order_info["_links"].get("results", [])
    if not download_links:
        console.print("[⚠️] No downloadable files found.")
        return

    if order_type == "PSScope" and num_bands == "four_bands":
        console.print(f"[🔍] Processing PSScope Order - Organizing by week...")
        image_metadata = []
        processed_filenames = set()
        for link in download_links:
            filename = Path(link.get("name", "")).name
            if filename in processed_filenames:
                continue
            processed_filenames.add(filename)
            if not filename.lower().endswith('.tif') or 'udm' in filename.lower() or filename.lower().endswith('.xml'):
                continue
            date_str = extract_date_from_filename(filename)
            if not date_str:
                console.print(f"[yellow]⚠️ Could not extract date from filename: {filename}[/yellow]")
                continue
            week_start = get_week_start_date(date_str)
            scene_id = extract_scene_id(filename) or "unknown"
            cloud_cover = 0
            image_metadata.append({
                'filename': filename,
                'date': date_str,
                'week_start': week_start, 
                'scene_id': scene_id,
                'cloud_cover': cloud_cover,
                'url': link.get('location'),
                'size': link.get('length', 0)
            })
        weeks = {}
        for img in sorted(image_metadata, key=lambda x: (x['week_start'], x['cloud_cover'], x['date'])):
            week = img['week_start']
            if week not in weeks:
                weeks[week] = img
        console.print(f"[✅] Found {len(image_metadata)} images across {len(weeks)} weeks")
        s3_path_prefix = f"planetscope analytic/four_bands/{aoi_name}"
        for week, img in weeks.items():
            filename = img['filename']
            s3_key = f"{s3_path_prefix}/{img['date']}_{img['scene_id']}.tiff"
            console.print(f"[⬆️] Uploading week {week} image: {filename} -> s3://{S3_BUCKET}/{s3_key}")
            r = requests.get(img['url'], stream=True)
            if r.status_code == 200:
                try:
                    s3.upload_fileobj(
                        io.BytesIO(r.content),
                        S3_BUCKET,
                        s3_key
                    )
                    console.print(f"[✅] Successfully uploaded to S3: s3://{S3_BUCKET}/{s3_key}")
                except Exception as e:
                    console.print(f"[❌] Error uploading to S3: {str(e)}", style="bold red")
            else:
                console.print(f"[❌] Failed to download image: {r.status_code}", style="bold red")
    elif is_basemap or order_type == "Basemap (Composite)":
        mosaic_parts = mosaic_name.split("_")
        if len(mosaic_parts) >= 4 and len(mosaic_parts[2]) == 4:
            mosaic_date = f"{mosaic_parts[2]}_{mosaic_parts[3]}"
        else:
            mosaic_date = "unknown_date"
        s3_path_prefix = f"basemaps/{aoi_name}/{mosaic_date}"
        console.print(f"[⬆️] Uploading Basemap files to S3 path: s3://{S3_BUCKET}/{s3_path_prefix}")
        for link in download_links:
            filename = Path(link.get("name", "")).name
            s3_key = f"{s3_path_prefix}/{filename}"
            console.print(f"[⬆️] Downloading and uploading: {filename}")
            r = requests.get(link.get('location'), stream=True)
            if r.status_code == 200:
                try:
                    s3.upload_fileobj(
                        io.BytesIO(r.content),
                        S3_BUCKET,
                        s3_key
                    )
                    console.print(f"[✅] Successfully uploaded to S3: s3://{S3_BUCKET}/{s3_key}")
                except Exception as e:
                    console.print(f"[❌] Error uploading to S3: {str(e)}", style="bold red")
            else:
                console.print(f"[❌] Failed to download file: {r.status_code}", style="bold red")
    try:
        metadata_json = json.dumps(order_info, indent=2)
        s3_metadata_path = ""
        if is_basemap or order_type == "Basemap (Composite)":
            s3_metadata_path = f"basemaps/{aoi_name}/{mosaic_date}/metadata.json"
        else:
            s3_metadata_path = f"planetscope analytic/four_bands/{aoi_name}/metadata.json"
        s3.put_object(
            Body=metadata_json,
            Bucket=S3_BUCKET,
            Key=s3_metadata_path
        )
        console.print(f"[✅] Order metadata saved to S3: s3://{S3_BUCKET}/{s3_metadata_path}")
    except Exception as e:
        console.print(f"[❌] Error saving metadata to S3: {str(e)}", style="bold red")

    console.print(f"[🎉] Order processing complete! All files uploaded to S3.", style="bold green")


@cli.command()
@click.argument("batch_id")
@click.option("--api-key", default=os.getenv("PL_API_KEY"), help="Planet API Key")
@click.option("--overwrite", is_flag=True, default=False, help="Re-download even if files already exist (default: skip existing files)")
@click.option("--output", default="s3", help="Output location: 's3' (default) or local directory path")
def batch_check_status(batch_id, api_key, overwrite, output):
    """
    Check status and download all orders in a batch.
    
    Finds all orders with the given batch_id from orders.json and processes each one.
    
    By default, files that already exist at the output location are skipped.
    Use --overwrite to re-download them.
    
    Use --output to specify where to save files:
    - 's3' (default): Upload to S3 bucket
    - Local path (e.g., './downloads'): Save files locally
    """
    if not api_key:
        console.print("[red]Error: API key is missing. Set PL_API_KEY env var or use --api-key.[/red]")
        return
    
    if not ORDERS_LOG_FILE.exists():
        console.print(f"[red]Error: {ORDERS_LOG_FILE} not found.[/red]")
        return
    
    try:
        with ORDERS_LOG_FILE.open("r") as f:
            orders = json.load(f)
    except json.JSONDecodeError as e:
        console.print(f"[red]Error reading {ORDERS_LOG_FILE}: {e}[/red]")
        return
    
    # Find all orders with this batch_id
    batch_orders = [o for o in orders if o.get("batch_id") == batch_id]
    
    if not batch_orders:
        console.print(f"[yellow]No orders found with batch_id: {batch_id}[/yellow]")
        console.print("[dim]Available batch_ids in orders.json:[/dim]")
        batch_ids = set(o.get("batch_id") for o in orders if o.get("batch_id"))
        if batch_ids:
            for bid in sorted(batch_ids):
                count = sum(1 for o in orders if o.get("batch_id") == bid)
                console.print(f"  • {bid} ({count} orders)")
        else:
            console.print("  (none found)")
        return
    
    console.print(f"[bold blue]📦 Found {len(batch_orders)} orders in batch: {batch_id}[/bold blue]\n")
    
    # Process each order
    results = {
        "success": [],
        "pending": [],
        "failed": []
    }
    
    for i, order in enumerate(batch_orders, 1):
        order_id = order.get("order_id")
        aoi_name = order.get("aoi_name", "Unknown")
        start_date = order.get("start_date", "N/A")
        end_date = order.get("end_date", "N/A")
        
        console.print(f"[{i}/{len(batch_orders)}] Checking {aoi_name} ({start_date} to {end_date})...")
        console.print(f"  Order ID: {order_id}")
        
        # Check order status
        response = requests.get(f"https://api.planet.com/compute/ops/orders/v2/{order_id}", auth=(api_key, ""))
        
        if response.status_code != 200:
            console.print(f"  [❌] Error checking order status: {response.text[:100]}", style="bold red")
            results["failed"].append({"order_id": order_id, "error": response.text[:100]})
            continue
        
        order_info = response.json()
        order_state = order_info["state"]
        console.print(f"  [✅] Status: {order_state}")
        
        if order_state != "success":
            console.print(f"  [⏳] Order not ready yet (state: {order_state})[/⏳]")
            results["pending"].append({"order_id": order_id, "state": order_state})
            continue
        
        # Order is ready - process it (reuse logic from check_order_status)
        aoi_name_normalized = normalize_aoi_name(order.get("aoi_name", "UnknownAOI"))
        mosaic_name = order.get("mosaic_name", "unknown_mosaic")
        order_type = order.get("order_type", "Unknown")
        num_bands = order.get("num_bands", "four_bands")
        product_bundle = order.get("product_bundle")
        
        is_basemap = "source_type" in order_info and order_info["source_type"] == "basemaps"
        
        download_links = order_info["_links"].get("results", [])
        if not download_links:
            console.print("  [⚠️] No downloadable files found.")
            results["failed"].append({"order_id": order_id, "error": "No downloadable files"})
            continue
        
        # Determine output location
        use_s3 = output.lower() == "s3"
        if not use_s3:
            local_output_dir = Path(output)
            local_output_dir.mkdir(parents=True, exist_ok=True)
        
        # Process and upload/save files (same logic as check_order_status)
        try:
            if order_type == "PSScope" and num_bands == "four_bands":
                console.print(f"  [🔍] Processing PSScope Order - Organizing by week...")
                image_metadata = []
                processed_filenames = set()
                for link in download_links:
                    filename = Path(link.get("name", "")).name
                    if filename in processed_filenames:
                        continue
                    processed_filenames.add(filename)
                    if not filename.lower().endswith('.tif') or 'udm' in filename.lower() or filename.lower().endswith('.xml'):
                        continue
                    date_str = extract_date_from_filename(filename)
                    if not date_str:
                        continue
                    week_start = get_week_start_date(date_str)
                    scene_id = extract_scene_id(filename) or "unknown"
                    image_metadata.append({
                        'filename': filename,
                        'date': date_str,
                        'week_start': week_start, 
                        'scene_id': scene_id,
                        'url': link.get('location'),
                        'size': link.get('length', 0)
                    })
                weeks = {}
                for img in sorted(image_metadata, key=lambda x: (x['week_start'], x['date'])):
                    week = img['week_start']
                    if week not in weeks:
                        weeks[week] = img
                console.print(f"  [✅] Found {len(image_metadata)} images across {len(weeks)} weeks")
                
                relative_path = f"planetscope analytic/four_bands/{aoi_name_normalized}"
                
                for week, img in weeks.items():
                    target_filename = f"{img['date']}_{img['scene_id']}.tiff"
                    
                    if use_s3:
                        s3_key = f"{relative_path}/{target_filename}"
                        # Check if file already exists in S3 (unless overwriting)
                        if not overwrite and s3_key_exists(S3_BUCKET, s3_key):
                            console.print(f"  [⏭️] Skipping (exists): s3://{S3_BUCKET}/{s3_key}")
                            continue
                        console.print(f"  [⬆️] Uploading: {img['filename']} -> s3://{S3_BUCKET}/{s3_key}")
                    else:
                        local_path = local_output_dir / relative_path / target_filename
                        # Check if file already exists locally (unless overwriting)
                        if not overwrite and local_path.exists():
                            console.print(f"  [⏭️] Skipping (exists): {local_path}")
                            continue
                        local_path.parent.mkdir(parents=True, exist_ok=True)
                        console.print(f"  [⬇️] Downloading: {img['filename']} -> {local_path}")
                    
                    r = requests.get(img['url'], stream=True)
                    if r.status_code == 200:
                        try:
                            if use_s3:
                                s3.upload_fileobj(
                                    io.BytesIO(r.content),
                                    S3_BUCKET,
                                    s3_key
                                )
                            else:
                                with open(local_path, 'wb') as f:
                                    f.write(r.content)
                            console.print(f"  [✅] Saved successfully")
                        except Exception as e:
                            console.print(f"  [❌] Error saving: {str(e)}", style="bold red")
                    else:
                        console.print(f"  [❌] Failed to download: {r.status_code}", style="bold red")
                        
            elif is_basemap or order_type == "Basemap (Composite)":
                mosaic_parts = mosaic_name.split("_")
                if len(mosaic_parts) >= 4 and len(mosaic_parts[2]) == 4:
                    mosaic_date = f"{mosaic_parts[2]}_{mosaic_parts[3]}"
                else:
                    mosaic_date = "unknown_date"
                    
                relative_path = f"basemaps/{aoi_name_normalized}/{mosaic_date}"
                
                if use_s3:
                    console.print(f"  [⬆️] Uploading Basemap files to s3://{S3_BUCKET}/{relative_path}")
                else:
                    console.print(f"  [⬇️] Downloading Basemap files to {local_output_dir / relative_path}")
                
                for link in download_links:
                    filename = Path(link.get("name", "")).name
                    
                    if use_s3:
                        s3_key = f"{relative_path}/{filename}"
                        # Check if file already exists in S3 (unless overwriting)
                        if not overwrite and s3_key_exists(S3_BUCKET, s3_key):
                            console.print(f"  [⏭️] Skipping (exists): s3://{S3_BUCKET}/{s3_key}")
                            continue
                        console.print(f"  [⬆️] Uploading: {filename}")
                    else:
                        local_path = local_output_dir / relative_path / filename
                        # Check if file already exists locally (unless overwriting)
                        if not overwrite and local_path.exists():
                            console.print(f"  [⏭️] Skipping (exists): {local_path}")
                            continue
                        local_path.parent.mkdir(parents=True, exist_ok=True)
                        console.print(f"  [⬇️] Downloading: {filename}")
                    
                    r = requests.get(link.get('location'), stream=True)
                    if r.status_code == 200:
                        try:
                            if use_s3:
                                s3.upload_fileobj(
                                    io.BytesIO(r.content),
                                    S3_BUCKET,
                                    s3_key
                                )
                            else:
                                with open(local_path, 'wb') as f:
                                    f.write(r.content)
                            console.print(f"  [✅] Saved successfully")
                        except Exception as e:
                            console.print(f"  [❌] Error saving: {str(e)}", style="bold red")
                    else:
                        console.print(f"  [❌] Failed to download: {r.status_code}", style="bold red")
            
            # Save metadata
            metadata_json = json.dumps(order_info, indent=2)
            if is_basemap or order_type == "Basemap (Composite)":
                metadata_relative_path = f"basemaps/{aoi_name_normalized}/{mosaic_date}/metadata.json"
            else:
                metadata_relative_path = f"planetscope analytic/four_bands/{aoi_name_normalized}/metadata.json"
            
            if use_s3:
                s3.put_object(
                    Body=metadata_json,
                    Bucket=S3_BUCKET,
                    Key=metadata_relative_path
                )
                console.print(f"  [✅] Metadata saved to S3")
            else:
                metadata_local_path = local_output_dir / metadata_relative_path
                metadata_local_path.parent.mkdir(parents=True, exist_ok=True)
                with open(metadata_local_path, 'w') as f:
                    f.write(metadata_json)
                console.print(f"  [✅] Metadata saved locally")
            
            console.print(f"  [🎉] Order complete!\n")
            results["success"].append(order)
        except Exception as e:
            console.print(f"  [❌] Error processing order: {str(e)}", style="bold red")
            results["failed"].append({"order_id": order_id, "error": str(e)})
    
    # Summary
    console.print("\n" + "="*60)
    console.print("[bold]📊 Batch Status Check Summary[/bold]")
    console.print("="*60)
    console.print(f"[green]Completed & Uploaded: {len(results['success'])}[/green]")
    if results["pending"]:
        console.print(f"[yellow]Pending (not ready): {len(results['pending'])}[/yellow]")
        for item in results["pending"]:
            console.print(f"  - {item['order_id'][:8]}... ({item.get('state', 'unknown')})")
    if results["failed"]:
        console.print(f"[red]Failed: {len(results['failed'])}[/red]")
        for item in results["failed"]:
            console.print(f"  - {item.get('order_id', 'unknown')[:8]}...: {item.get('error', 'Unknown')[:50]}")
    
    if results["pending"]:
        console.print(f"\n[yellow]💡 Run again later to check pending orders.[/yellow]")


@cli.command()
@click.option("--geojson", required=True, type=click.Path(exists=True), help="Path to AOI GeoJSON")
@click.option("--start-date", required=True, help="Start date (YYYY-MM-DD)")
@click.option("--end-date", required=True, help="End date (YYYY-MM-DD)")
@click.option("--num-bands", type=click.Choice(['four_bands', 'eight_bands']), default='four_bands', help="Choose 4B or 8B imagery")
@click.option("--bundle", default=None, help="Override bundle name to use")
@click.option("--cadence", type=click.Choice(["daily", "weekly", "monthly"]), default="weekly", help="Scene selection cadence")
@click.option("--api-key", default=os.getenv("PL_API_KEY"), help="Planet API Key")
def search_scenes(geojson, start_date, end_date, num_bands, bundle, cadence, api_key):
    gdf = gpd.read_file(geojson)
    gdf = gdf.to_crs(epsg=4326)
    aoi_geom = gdf.geometry.union_all()

    gdf_equal_area = gdf.to_crs(epsg=6933)  # World Cylindrical Equal Area
    # Compute area in sq km using equal-area CRS
    aoi_area_sqkm = gdf_equal_area.area.sum() / 1e6  # m² → km²
    console.print(f"[✓] AOI area: {aoi_area_sqkm:.2f} sq km", style="bold blue")

    start_iso = f"{start_date}T00:00:00Z"
    end_iso = f"{end_date}T23:59:59Z"

    start_year = int(start_date.split('-')[0])

    if bundle:
        product_bundle = bundle
        console.print(f"[✅] Using override bundle: {product_bundle}", style="bold blue")
    elif num_bands == "four_bands":
        product_bundle = "ortho_analytic_4b_sr"
        console.print(f"[✅] Using 4-band surface reflectance: {product_bundle}", style="bold blue")
    else:
        product_bundle = "ortho_analytic_8b_sr" if start_year >= 2021 else "analytic_sr_udm2"
        console.print(f"[✅] Using 8-band surface reflectance: {product_bundle}", style="bold blue")

    payload = {
        "item_types": ["PSScene"],
        "filter": {
            "type": "AndFilter",
            "config": [
                {"type": "GeometryFilter", "field_name": "geometry", "config": aoi_geom.__geo_interface__},
                {"type": "DateRangeFilter", "field_name": "acquired", "config": {"gte": start_iso, "lte": end_iso}},
                {"type": "RangeFilter", "field_name": "cloud_cover", "config": {"lte": 0.0}},
                {"type": "AssetFilter", "config": [product_bundle]},
                {"type":"StringInFilter", "field_name":"quality_category", "config":["standard"]}
            ]
        }
    }

    search_url = "https://api.planet.com/data/v1/quick-search"
    search_headers = {"Content-Type": "application/json"}
    
    # Fetch all results with pagination
    try:
        features = fetch_all_search_results(search_url, payload, api_key, search_headers)
    except Exception as e:
        console.print(f"[red]Search failed: {str(e)}[/red]")
        return

    if not features:
        console.print("[yellow]No scenes found.[/yellow]")
        return
    
    console.print(f"[✓] Found {len(features)} scenes matching initial criteria.", style="bold blue")

    def get_interval_key(date_obj):
        if cadence == "daily":
            return date_obj.strftime("%Y-%m-%d")
        elif cadence == "weekly":
            sunday = date_obj - timedelta(days=date_obj.weekday() + 1 if date_obj.weekday() != 6 else 0)
            return sunday.strftime("%Y-%m-%d")
        elif cadence == "monthly":
            return date_obj.strftime("%Y-%m")

    scene_groups = defaultdict(list)
    for f in features:
        props = f["properties"]
        geom = shape(f["geometry"])
        fid = f["id"]
        intersect_area = geom.intersection(aoi_geom).area
        coverage_pct = (intersect_area / aoi_geom.area) * 100
        if coverage_pct < MIN_COV_PCT:
            console.print(f"[dim]Skipping {fid}: only {coverage_pct:.2f}% coverage[/dim]")
            continue

        date = datetime.strptime(props["acquired"][:10], "%Y-%m-%d")
        key = get_interval_key(date)
        scene_groups[key].append((coverage_pct, date, f))

        # Sort each group by coverage descending, then date ascending; select first
        selected = []
        for group in scene_groups.values():
            group.sort(key=lambda x: (-x[0], x[1]))  # coverage desc, date asc
            coverage_pct, date, f = group[0]
            selected.append((f, coverage_pct, date))



    console.print(f"[green]Selected {len(selected)} best scenes ({cadence})[/green]")
    for f, cov, dt in selected:
        thumb = f["_links"].get("thumbnail")
        console.print(f"{dt.date()} | ID: {f['id']} | Coverage: {cov:.2f}% | [link={thumb}]thumbnail[/link]")

    # Print the total sqkm covered by this order and ask the user if they want to proceed
    console.print(f"[blue]Total quota used in this order: {aoi_area_sqkm * len(selected):.2f} sq km[/blue]")

    ids = ",".join([f["id"] for f, _, _ in selected])
    console.print(f"\nUse this to order: [bold blue]--scene-ids {ids}[/bold blue]")


@cli.command()
@click.option("--shp", required=True, type=click.Path(exists=True), help="Path to input Shapefile with AOIs and attributes")
@click.option("--gage-id-col", default="gage_id", help="Column name for gage ID (default: gage_id)")
@click.option("--start-date-col", default="start_date", help="Column name for start date (default: start_date)")
@click.option("--end-date-col", default="end_date", help="Column name for end date (default: end_date)")
@click.option("--num-bands", type=click.Choice(['four_bands', 'eight_bands']), default='four_bands', help="Choose 4B or 8B imagery")
@click.option("--api-key", default=os.getenv("PL_API_KEY"), help="Planet API Key")
@click.option("--bundle", default=None, help="Override bundle name to use")
@click.option("--cadence", type=click.Choice(["daily", "weekly", "monthly"]), default="weekly", help="Scene selection cadence")
@click.option("--max-months", default=6, type=int, help="Maximum months per order chunk (default: 6)")
@click.option("--dry-run", is_flag=True, help="Preview orders without submitting")
def batch_submit(shp, gage_id_col, start_date_col, end_date_col, num_bands, api_key, bundle, cadence, max_months, dry_run):
    """
    Submit multiple PlanetScope orders from a shapefile.
    
    The shapefile should contain:
    - Geometry: AOI polygons for each gage
    - gage_id column: Unique identifier for each gage
    - start_date column: Start date (YYYY-MM-DD) for each gage
    - end_date column: End date (YYYY-MM-DD) for each gage
    
    Date ranges longer than max-months will be automatically subdivided.
    """
    if not api_key:
        console.print("[red]Error: API key is missing. Set PL_API_KEY env var or use --api-key.[/red]")
        return
    
    try:
        # Read shapefile
        gdf = gpd.read_file(shp)
        gdf = gdf.to_crs(epsg=4326)
        original_crs = gdf.crs
        # Prepare equal-area CRS for accurate area calculations
        equal_area_crs = "EPSG:6933"

        console.print(f"[bold blue]📂 Loaded shapefile with {len(gdf)} features[/bold blue]")
        console.print(f"[dim]Columns: {', '.join(gdf.columns.tolist())}[/dim]")
        
        # Validate required columns
        required_cols = [gage_id_col, start_date_col, end_date_col]
        missing_cols = [col for col in required_cols if col not in gdf.columns]
        if missing_cols:
            console.print(f"[red]Error: Missing required columns: {missing_cols}[/red]")
            console.print(f"[yellow]Available columns: {gdf.columns.tolist()}[/yellow]")
            return
        
        # Prepare orders
        all_orders = []
        for idx, row in gdf.iterrows():
            gage_id = str(row[gage_id_col])
            start_date = str(row[start_date_col])
            end_date = str(row[end_date_col])
            
            # Parse dates - handle various formats
            try:
                # Try parsing to validate dates
                start_dt = datetime.strptime(start_date.strip(), "%Y-%m-%d")
                end_dt = datetime.strptime(end_date.strip(), "%Y-%m-%d")
                start_date = start_dt.strftime("%Y-%m-%d")
                end_date = end_dt.strftime("%Y-%m-%d")
            except ValueError as e:
                console.print(f"[yellow]⚠️ Skipping {gage_id}: Invalid date format ({e})[/yellow]")
                continue
            
            # Subdivide if needed
            date_chunks = subdivide_date_range(start_date, end_date, max_months)
            
            for chunk_start, chunk_end in date_chunks:
                all_orders.append({
                    "gage_id": gage_id,
                    "start_date": chunk_start,
                    "end_date": chunk_end,
                    "geometry": row.geometry,
                    "row_idx": idx
                })
        
        console.print(f"\n[bold green]📋 Prepared {len(all_orders)} orders from {len(gdf)} gages[/bold green]")
        
        # Show order summary
        console.print("\n[bold]Order Summary:[/bold]")
        gage_order_counts = defaultdict(int)
        for order in all_orders:
            gage_order_counts[order["gage_id"]] += 1
        
        for gage_id, count in gage_order_counts.items():
            if count > 1:
                console.print(f"  • {gage_id}: {count} orders (date range subdivided)")
            else:
                console.print(f"  • {gage_id}: {count} order")
        
        # Generate batch_id for this batch submission
        batch_id = str(uuid.uuid4())
        console.print(f"\n[bold cyan]📦 Batch ID: {batch_id}[/bold cyan]")
        console.print("[dim]Use this ID to check status of all orders in this batch: batch-check-status[/dim]")
        
        if dry_run:
            console.print("\n[bold yellow]🔍 DRY RUN MODE - No orders will be submitted[/bold yellow]")
        
        # Determine product bundle
        if bundle:
            product_bundle = bundle
            console.print(f"\n[✅] Using override bundle: {product_bundle}")
        elif num_bands == "four_bands":
            product_bundle = "ortho_analytic_4b_sr"
            console.print(f"\n[✅] Using 4-band surface reflectance: {product_bundle}")
        else:
            # For 8-band, use the earliest year to determine bundle
            #TODO: Fix handling of 8-band data to return error if user requests 8-band but dates are before 2021
            earliest_year = min(int(o["start_date"].split('-')[0]) for o in all_orders)
            product_bundle = "ortho_analytic_8b_sr" if earliest_year >= 2021 else "ortho_analytic_4b_sr"
            console.print(f"\n[✅] Using 8-band surface reflectance: {product_bundle}")

        # Cross walk product bundle for ordering
        if product_bundle == "ortho_analytic_4b_sr":
            product_bundle_order = "analytic_sr_udm2"
        elif product_bundle == "ortho_analytic_8b_sr":
            product_bundle_order = "analytic_8b_sr_udm2"
        else:
            product_bundle_order = product_bundle
        
        # Process orders
        results = {
            "submitted": [],
            "failed": [],
            "no_scenes": []
        }
        
        console.print("\n[bold]Processing orders...[/bold]\n")
        
        for i, order in enumerate(all_orders, 1):
            gage_id = order["gage_id"]
            start_date = order["start_date"]
            end_date = order["end_date"]
            geom = order["geometry"]
            
            console.print(f"[{i}/{len(all_orders)}] {gage_id}: {start_date} to {end_date}...", end=" ")
            
            # Prepare geometry
            aoi_geom = geom
            aoi_geojson = aoi_geom.__geo_interface__
            
            geom_equal_area = gpd.GeoSeries([geom], crs=original_crs).to_crs(equal_area_crs)
            aoi_area_sqkm = geom_equal_area.area.iloc[0] / 1e6  # m² → km²

            
            result = submit_single_order(
                aoi_geom=aoi_geom,
                aoi_geojson=aoi_geojson,
                aoi_area_sqkm=aoi_area_sqkm,
                start_date=start_date,
                end_date=end_date,
                gage_id=gage_id,
                num_bands=num_bands,
                product_bundle=product_bundle,
                product_bundle_order=product_bundle_order,
                cadence=cadence,
                api_key=api_key,
                dry_run=dry_run,
                batch_id=batch_id
            )
            
            if result.get("success"):
                scenes_found = result.get('scenes_found', 0)
                scenes_selected = result.get('scenes_selected', 0)
                quota_ha = result.get('quota_hectares', 0)
                if dry_run:
                    console.print(f"[green]✓ Would submit ({scenes_found} found, {scenes_selected} selected, {quota_ha:,.0f} ha quota)[/green]")
                else:
                    console.print(f"[green]✓ Order {result['order_id'][:8]}... ({scenes_found} found, {scenes_selected} selected, {quota_ha:,.0f} ha quota)[/green]")
                results["submitted"].append(result)
            elif "No cloud-free" in result.get("error", "") or "No full-coverage" in result.get("error", ""):
                console.print(f"[yellow]⚠ No valid scenes[/yellow]")
                results["no_scenes"].append({**order, **result})
            else:
                console.print(f"[red]✗ {result.get('error', 'Unknown error')[:50]}...[/red]")
                results["failed"].append({**order, **result})
        
        # Summary
        console.print("\n" + "="*60)
        console.print("[bold]📊 Batch Order Summary[/bold]")
        console.print("="*60)
        
        # Calculate totals for submitted orders
        total_scenes_found = sum(r.get('scenes_found', 0) for r in results['submitted'])
        total_scenes_selected = sum(r.get('scenes_selected', 0) for r in results['submitted'])
        total_quota_hectares = sum(r.get('quota_hectares', 0) for r in results['submitted'])
        
        if dry_run:
            console.print(f"[green]Would submit: {len(results['submitted'])} orders ({total_scenes_found} found, {total_scenes_selected} selected, {total_quota_hectares:,.0f} ha quota)[/green]")
        else:
            console.print(f"[green]Submitted: {len(results['submitted'])} orders ({total_scenes_found} found, {total_scenes_selected} selected, {total_quota_hectares:,.0f} ha quota)[/green]")
        
        if results["no_scenes"]:
            console.print(f"[yellow]No valid scenes: {len(results['no_scenes'])} orders[/yellow]")
            for item in results["no_scenes"]:
                console.print(f"  - {item['gage_id']}: {item['start_date']} to {item['end_date']}")
        
        if results["failed"]:
            console.print(f"[red]Failed: {len(results['failed'])} orders[/red]")
            for item in results["failed"]:
                console.print(f"  - {item['gage_id']}: {item.get('error', 'Unknown')}")
        
        if not dry_run and results["submitted"]:
            console.print(f"\n[bold green]🎉 Successfully submitted {len(results['submitted'])} orders![/bold green]")
            console.print(f"[bold blue]📈 Total: {total_scenes_found} found, {total_scenes_selected} selected, {total_quota_hectares:,.0f} hectares of quota[/bold blue]")
            console.print(f"[bold cyan]📦 Batch ID: {batch_id}[/bold cyan]")
            console.print(f"[dim]Check all orders in this batch: python main.py batch-check-status {batch_id}[/dim]")
            console.print("[dim]Or check individual orders: check-order-status <order_id>[/dim]")
        
    except Exception as e:
        console.print(f"[red]Error: {str(e)}[/red]")
        import traceback
        console.print(f"[dim]{traceback.format_exc()}[/dim]")
        sys.exit(1)


@cli.command()
@click.option("--start-date", required=True, help="Start date (YYYY-MM-DD)")
@click.option("--end-date", required=True, help="End date (YYYY-MM-DD)")
@click.option("--api-key", default=os.getenv("PL_API_KEY"), help="Planet API Key")
def list_basemaps(start_date, end_date, api_key):
    """List available basemaps within a date range."""
    if not api_key:
        console.print("[red]Error: API key is missing.[/red]")
        return

    url = "https://api.planet.com/basemaps/v1/mosaics"
    all_mosaics = []

    while url:
        response = requests.get(url, auth=(api_key, ""))
        if response.status_code != 200:
            console.print(f"[red]Error fetching basemaps: {response.text}[/red]")
            return

        data = response.json()
        mosaics = data.get("mosaics", [])
        all_mosaics.extend(mosaics)

        url = data["_links"].get("_next") if "_links" in data else None

    console.print(f"[cyan]Total basemaps found: {len(all_mosaics)}[/cyan]")

    filtered_mosaics = [
        m for m in all_mosaics
        if start_date <= m["first_acquired"][:10] <= end_date
    ]
    console.print(f"[blue]Basemaps count after filtering: {len(filtered_mosaics)}[/blue]")
    if not filtered_mosaics:
        console.print("[yellow]No matching basemaps found.[/yellow]")
        return

    console.print("[green]Matching Basemaps:[/green]")
    for mosaic in filtered_mosaics:
        console.print(f"Mosaic Name: {mosaic['name']} | ID: {mosaic['id']} | Acquired: {mosaic['first_acquired']}")

# --- CLI Registration and Main ---

cli.add_command(convert_shp)
cli.add_command(order_basemap)
cli.add_command(submit)
cli.add_command(batch_submit)
cli.add_command(check_order_status)
cli.add_command(batch_check_status)
cli.add_command(list_basemaps)
cli.add_command(generate_aoi)
cli.add_command(search_scenes)

if __name__ == '__main__':
    cli()