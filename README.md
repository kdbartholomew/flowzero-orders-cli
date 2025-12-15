# FlowZero Orders CLI

A command-line tool for ordering Planet Labs satellite imagery to support water detection and river monitoring workflows.

## Overview

This CLI streamlines the process of:
- Creating Areas of Interest (AOIs) for river monitoring
- Ordering PlanetScope imagery with automatic scene selection
- Ordering Planet Basemap composites
- Batch ordering multiple AOIs with per-gage date ranges
- Uploading completed orders to S3 for downstream processing

## Installation

### Prerequisites
- Python 3.8+
- Planet Labs API key
- AWS credentials (for S3 uploads)

### Setup

1. Clone the repository:
```bash
git clone <repository-url>
cd flowzero-orders-cli
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Create a `.env` file with your credentials:
```env
PL_API_KEY=your_planet_api_key
AWS_ACCESS_KEY_ID=your_aws_access_key
AWS_SECRET_ACCESS_KEY=your_aws_secret_key
```

## Commands

### `generate-aoi`
Launch an interactive web interface to draw and save AOIs.

```bash
python main.py generate-aoi
```
Opens a browser at `http://localhost:5000` with a map interface for drawing polygons.

---

### `convert-shp`
Convert a Shapefile to GeoJSON format.

```bash
python main.py convert-shp --shp path/to/shapefile.shp --output ./geojsons
```

| Option | Default | Description |
|--------|---------|-------------|
| `--shp` | required | Path to input Shapefile |
| `--output` | `./geojsons` | Output directory for GeoJSON |

---

### `submit`
Submit a single PlanetScope imagery order for one AOI and date range.

```bash
python main.py submit \
  --geojson ./geojsons/my_aoi.geojson \
  --start-date 2024-01-01 \
  --end-date 2024-06-30 \
  --cadence weekly
```

| Option | Default | Description |
|--------|---------|-------------|
| `--geojson` | required | Path to AOI GeoJSON file |
| `--start-date` | required | Start date (YYYY-MM-DD) |
| `--end-date` | required | End date (YYYY-MM-DD) |
| `--num-bands` | `four_bands` | `four_bands` or `eight_bands` |
| `--cadence` | `weekly` | Scene selection: `daily`, `weekly`, or `monthly` |
| `--bundle` | auto | Override product bundle name |
| `--api-key` | env var | Planet API key |

**Scene Selection Logic:**
- Filters for 0% cloud cover
- Requires ≥99% AOI coverage
- Selects best scene per cadence interval

---

### `batch-submit` ⭐ NEW
Submit multiple PlanetScope orders from a single shapefile containing multiple AOIs with per-gage date ranges.

```bash
python main.py batch-submit \
  --shp ./gages_with_dates.shp \
  --gage-id-col "GageID" \
  --start-date-col "StartDate" \
  --end-date-col "EndDate" \
  --cadence weekly
```

| Option | Default | Description |
|--------|---------|-------------|
| `--shp` | required | Path to Shapefile with AOIs and attributes |
| `--gage-id-col` | `gage_id` | Column name for gage identifier |
| `--start-date-col` | `start_date` | Column name for start date |
| `--end-date-col` | `end_date` | Column name for end date |
| `--num-bands` | `four_bands` | `four_bands` or `eight_bands` |
| `--cadence` | `weekly` | Scene selection: `daily`, `weekly`, or `monthly` |
| `--max-months` | `9` | Maximum months per order (auto-subdivides longer ranges) |
| `--bundle` | auto | Override product bundle name |
| `--dry-run` | false | Preview orders without submitting |
| `--api-key` | env var | Planet API key |

**Required Shapefile Columns:**
- Geometry column with AOI polygons
- Gage ID column (unique identifier)
- Start date column (YYYY-MM-DD format)
- End date column (YYYY-MM-DD format)

**Automatic Date Subdivision:**
Date ranges longer than `--max-months` (default 9) are automatically split into multiple orders. For example, a 2-year date range becomes three orders: 9 months + 9 months + 6 months.

**Example Output:**
```
📂 Loaded shapefile with 5 features
Columns: gage_id, start_date, end_date, geometry

📋 Prepared 8 orders from 5 gages

Order Summary:
  • Gage_001: 2 orders (date range subdivided)
  • Gage_002: 1 order
  • Gage_003: 2 orders (date range subdivided)
  • Gage_004: 1 order
  • Gage_005: 2 orders (date range subdivided)

[✅] Using 4-band surface reflectance: analytic_sr_udm2

Processing orders...

[1/8] Gage_001: 2024-01-01 to 2024-09-30... ✓ Order a1b2c3d4... (15 scenes)
[2/8] Gage_001: 2024-10-01 to 2024-12-31... ✓ Order e5f6g7h8... (6 scenes)
...

============================================================
📊 Batch Order Summary
============================================================
Submitted: 7 orders
No valid scenes: 1 orders
  - Gage_004: 2024-06-01 to 2024-08-31

🎉 Successfully submitted 7 orders!
```

---

### `search-scenes`
Search for available PlanetScope scenes without placing an order.

```bash
python main.py search-scenes \
  --geojson ./geojsons/my_aoi.geojson \
  --start-date 2024-01-01 \
  --end-date 2024-03-31 \
  --cadence weekly
```

| Option | Default | Description |
|--------|---------|-------------|
| `--geojson` | required | Path to AOI GeoJSON |
| `--start-date` | required | Start date (YYYY-MM-DD) |
| `--end-date` | required | End date (YYYY-MM-DD) |
| `--cadence` | `weekly` | `daily`, `weekly`, or `monthly` |
| `--api-key` | env var | Planet API key |

---

### `list-basemaps`
List available Planet Basemap mosaics within a date range.

```bash
python main.py list-basemaps --start-date 2024-01-01 --end-date 2024-12-31
```

| Option | Default | Description |
|--------|---------|-------------|
| `--start-date` | required | Start date (YYYY-MM-DD) |
| `--end-date` | required | End date (YYYY-MM-DD) |
| `--api-key` | env var | Planet API key |

---

### `order-basemap`
Order a Planet Basemap composite for an AOI.

```bash
python main.py order-basemap \
  --mosaic-name global_monthly_2024_06_mosaic \
  --geojson ./geojsons/my_aoi.geojson
```

| Option | Default | Description |
|--------|---------|-------------|
| `--mosaic-name` | required | Mosaic name from `list-basemaps` |
| `--geojson` | required | Path to AOI GeoJSON |
| `--api-key` | env var | Planet API key |

---

### `check-order-status`
Check an order's status and upload completed files to S3.

```bash
python main.py check-order-status <order_id>
```

When an order is complete (`success` state), this command:
1. Downloads all imagery files
2. Organizes by date/week
3. Uploads to S3 bucket `flowzero`
4. Saves order metadata

---

## Workflow Examples

### Single AOI Order
```bash
# 1. Convert shapefile to GeoJSON
python main.py convert-shp --shp ./AOI_Shapefiles/MyRiver/MyRiver.shp

# 2. Search for available scenes (optional)
python main.py search-scenes --geojson ./geojsons/MyRiver.geojson \
  --start-date 2024-01-01 --end-date 2024-06-30

# 3. Submit order
python main.py submit --geojson ./geojsons/MyRiver.geojson \
  --start-date 2024-01-01 --end-date 2024-06-30

# 4. Check status and download
python main.py check-order-status <order_id>
```

### Batch Order (Multiple Gages)
```bash
# 1. Prepare shapefile with columns: gage_id, start_date, end_date, geometry

# 2. Preview orders (dry run)
python main.py batch-submit --shp ./all_gages.shp --dry-run

# 3. Submit all orders
python main.py batch-submit --shp ./all_gages.shp

# 4. Check each order status
python main.py check-order-status <order_id_1>
python main.py check-order-status <order_id_2>
# ...
```

---

## Project Structure

```
flowzero-orders-cli/
├── main.py              # CLI entry point and commands
├── generate_aoi.py      # Flask app for interactive AOI creation
├── requirements.txt     # Python dependencies
├── orders.json          # Log of all submitted orders
├── .env                 # Environment variables (not in repo)
├── AOI Shapefiles/      # Input shapefiles
│   ├── Salinas/
│   ├── SantaClara/
│   └── ...
└── geojsons/            # Converted GeoJSON files
    ├── DrySpy_AOI_SalinasRiver.geojson
    └── ...
```

---

## Order Logging

All orders are logged to `orders.json` with metadata:
```json
{
  "order_id": "abc123...",
  "aoi_name": "SalinasRiver",
  "order_type": "PSScope",
  "start_date": "2024-01-01",
  "end_date": "2024-06-30",
  "num_bands": "four_bands",
  "product_bundle": "analytic_sr_udm2",
  "scenes_selected": 15,
  "batch_order": true,
  "timestamp": "2024-12-15T10:30:00"
}
```

---

## S3 Output Structure

Completed orders are uploaded to S3 with this structure:
```
s3://flowzero/
├── planetscope analytic/
│   └── four_bands/
│       └── {aoi_name}/
│           ├── 2024_01_15_{scene_id}.tiff
│           ├── 2024_01_22_{scene_id}.tiff
│           └── metadata.json
└── basemaps/
    └── {aoi_name}/
        └── {year}_{month}/
            └── *.tiff
```

---

## Recent Changes

### Added: `batch-submit` Command
- **Purpose**: Submit multiple orders from a single shapefile with per-gage date ranges
- **Key Features**:
  - Reads shapefile with gage IDs, geometries, and individual date ranges
  - Automatically subdivides date ranges > 9 months into multiple orders
  - Progress tracking with success/failure summary
  - Dry-run mode for previewing without submitting
  - Configurable column names for flexibility with different shapefile schemas

### New Helper Functions
- `subdivide_date_range()`: Splits long date ranges into manageable chunks
- `submit_single_order()`: Reusable order submission logic

### New Dependency
- `python-dateutil`: For reliable month-based date arithmetic

---

## Troubleshooting

### "No cloud-free scenes found"
- Try expanding your date range
- Check if the AOI has frequent cloud cover during that period

### "No full-coverage scenes matched filter"
- Your AOI may be too large for single-scene coverage
- Consider splitting into smaller AOIs

### Date range subdivision
- Orders are limited to ~9 months to avoid Planet API limits
- Use `--max-months` to adjust if needed

---

## License

[Add license information]

