import sys
import os
import json
import re
import io
import mimetypes
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

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
        aoi_area_sqkm = gdf.area.sum() / 1e6

        console.print(f"[✓] AOI area: {aoi_area_sqkm:.2f} sq km", style="bold blue")

        start_year = int(start_date.split('-')[0])

        if bundle:
            product_bundle = bundle
            console.print(f"[✅] Using override bundle: {product_bundle}", style="bold blue")
        elif num_bands == "four_bands":
            product_bundle = "analytic_sr_udm2"
            console.print(f"[✅] Using 4-band surface reflectance: {product_bundle}", style="bold blue")
        else:
            product_bundle = "analytic_8b_sr_udm2" if start_year >= 2022 else "analytic_sr_udm2"
            console.print(f"[✅] Using 8-band surface reflectance: {product_bundle}", style="bold blue")

        # Perform scene search with cadence filtering (as in search-scenes)
        search_url = "https://api.planet.com/data/v1/quick-search"
        search_payload = {
            "item_types": ["PSScene"],
            "filter": {
                "type": "AndFilter",
                "config": [
                    {"type": "GeometryFilter", "field_name": "geometry", "config": aoi},
                    {"type": "DateRangeFilter", "field_name": "acquired", "config": {"gte": start_date_iso, "lte": end_date_iso}},
                    {"type": "RangeFilter", "field_name": "cloud_cover", "config": {"lte": 0.0}}
                ]
            }
        }

        search_headers = {"Content-Type": "application/json"}
        search_response = requests.post(search_url, json=search_payload, auth=(api_key, ""), headers=search_headers)

        if search_response.status_code != 200:
            console.print(f"❌ Failed to search for scenes: {search_response.text}", style="bold red")
            return

        features = search_response.json().get("features", [])
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
            if coverage_pct < 99:
                continue

            date_str = props["acquired"][:10]
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
            key = get_interval_key(date_obj)
            scene_groups[key].append((coverage_pct, feature))

        selected = []
        for key in sorted(scene_groups):
            best = sorted(scene_groups[key], key=lambda x: -x[0])[0]
            selected.append(best[1])

        if not selected:
            console.print("[yellow]No full-coverage scenes matched filter.[/yellow]")
            return

        console.print(f"[green]Selected {len(selected)} best scenes ({cadence})[/green]")
        item_ids = [f["id"] for f in selected]

        order_url = "https://api.planet.com/compute/ops/orders/v2"
        order_payload = {
            "name": f"PSScope Order {Path(geojson).stem}",
            "products": [{
                "item_ids": item_ids,
                "item_type": "PSScene",
                "product_bundle": product_bundle
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
                "product_bundle": product_bundle,
                "clipped": True,
                "aoi_area_sqkm": aoi_area_sqkm,
                "timestamp": datetime.now().isoformat()
            })
        else:
            console.print(f"❌ Order submission failed: {response.status_code} - {response.text[:100]}...", style="bold red")

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
@click.option("--geojson", required=True, type=click.Path(exists=True), help="Path to AOI GeoJSON")
@click.option("--start-date", required=True, help="Start date (YYYY-MM-DD)")
@click.option("--end-date", required=True, help="End date (YYYY-MM-DD)")
@click.option("--cadence", type=click.Choice(["daily", "weekly", "monthly"]), default="weekly", help="Scene selection cadence")
@click.option("--api-key", default=os.getenv("PL_API_KEY"), help="Planet API Key")
def search_scenes(geojson, start_date, end_date, cadence, api_key):
    gdf = gpd.read_file(geojson)
    gdf = gdf.to_crs(epsg=4326)
    aoi_geom = gdf.geometry.unary_union
    aoi_area = aoi_geom.area

    start_iso = f"{start_date}T00:00:00Z"
    end_iso = f"{end_date}T23:59:59Z"

    payload = {
        "item_types": ["PSScene"],
        "filter": {
            "type": "AndFilter",
            "config": [
                {"type": "GeometryFilter", "field_name": "geometry", "config": aoi_geom.__geo_interface__},
                {"type": "DateRangeFilter", "field_name": "acquired", "config": {"gte": start_iso, "lte": end_iso}},
                {"type": "RangeFilter", "field_name": "cloud_cover", "config": {"lte": 0.0}}
            ]
        }
    }

    response = requests.post(
        "https://api.planet.com/data/v1/quick-search",
        json=payload,
        auth=(api_key, ""),
        headers={"Content-Type": "application/json"}
    )
    if response.status_code != 200:
        console.print(f"[red]Search failed: {response.status_code} {response.text}[/red]")
        return

    features = response.json().get("features", [])
    if not features:
        console.print("[yellow]No scenes found.[/yellow]")
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
    for f in features:
        props = f["properties"]
        geom = shape(f["geometry"])
        intersect_area = geom.intersection(aoi_geom).area
        coverage_pct = (intersect_area / aoi_area) * 100
        if coverage_pct < 99:
            continue

        date = datetime.strptime(props["acquired"][:10], "%Y-%m-%d")
        key = get_interval_key(date)
        scene_groups[key].append((coverage_pct, f))

    selected = [sorted(group, key=lambda x: -x[0])[0][1] for group in scene_groups.values()]

    console.print(f"[green]Selected {len(selected)} best scenes ({cadence})[/green]")
    for f in selected:
        date = f["properties"]["acquired"][:10]
        thumb = f["_links"].get("thumbnail")
        console.print(f"{date} | ID: {f['id']} | [link={thumb}]thumbnail[/link]")

    ids = ",".join([f["id"] for f in selected])
    console.print(f"\nUse this to order: [bold blue]--scene-ids {ids}[/bold blue]")

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
cli.add_command(check_order_status)
cli.add_command(list_basemaps)
cli.add_command(generate_aoi)
cli.add_command(search_scenes)

if __name__ == '__main__':
    cli()