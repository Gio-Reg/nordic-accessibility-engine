import os
import shutil
import time
import requests
import geopandas as gpd
import osmnx as ox
import pandas as pd
import networkx as nx
from sqlalchemy import create_engine, text

# Configuration
PLACE_NAME = "Östersund, Sweden"
TARGET_CRS = "EPSG:3006"  # SWEREF 99 TM
DB_URL = "postgresql://postgres:postgres@localhost:5432/ostersund_accessibility"

# Configure OSMnx
ox.settings.log_console = True
ox.settings.use_cache = True
ox.settings.requests_timeout = 180
ox.settings.max_query_area_size = 5e9  # Push the limit even higher to accommodate the bounding area

# Clean list of Overpass endpoints
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/",
    "https://overpass.kumi.systems/api/",
    "https://maps.mail.ru/osm/tools/overpass/api/",
]

# Set global user agent once at launch
ox.settings.http_user_agent = "SRSAE-UrbanAccessibility-Pipeline/2.0 (research)"

def default_speed(highway_type):
    """Assign default Swedish speed limits (km/h) based on highway classification."""
    if isinstance(highway_type, list):
        highway_type = highway_type[0]

    speed_map = {
        "motorway": 110,
        "trunk": 90,
        "primary": 80,
        "secondary": 70,
        "tertiary": 60,
        "unclassified": 50,
        "residential": 40,
        "living_street": 30,
        "pedestrian": 15,
        "footway": 5,
        "path": 5,
    }
    return speed_map.get(str(highway_type), 50)


def fetch_graph_around_center(point, dist=5000):
    """Fetch network graph by requesting raw XML to bypass OSMnx query builders."""
    last_exception = None
    lat, lon = point
    
    # 1. Simple, robust Overpass QL asking for XML output (all highways)
    ql_query = f"""
    [out:xml][timeout:190];
    (
      way["highway"](around:{dist},{lat},{lon});
    );
    (._; >;);
    out body;
    """
    
    headers = {'User-Agent': 'SRSAE-UrbanAccessibility-Pipeline/2.0 (research)'}
    temp_file = "temp_ostersund.osm"
    
    for endpoint in OVERPASS_ENDPOINTS:
        print(f"--> Attempting raw XML download via: {endpoint}")
        try:
            # 2. Fetch raw XML from Overpass directly
            response = requests.post(
                endpoint + "interpreter", 
                data={"data": ql_query}, 
                headers=headers,
                timeout=200
            )
            
            if response.status_code == 200:
                # 3. Save the XML response to a local file
                with open(temp_file, "w", encoding="utf-8") as f:
                    f.write(response.text)
                
                print(f"    XML saved to {temp_file}. Parsing into NetworkX graph...")
                
                # 4. Load the graph directly from the XML file
                G = ox.graph_from_xml(temp_file, simplify=True)
                
                if G and len(G) > 0:
                    print(f"    Successfully built graph with {len(G.nodes())} nodes.")
                    # Clean up the temp file
                    if os.path.exists(temp_file):
                        os.remove(temp_file)
                    return G
                else:
                    print("    Graph parsed but contained 0 nodes.")
            else:
                print(f"    Server returned HTTP {response.status_code}")
                if response.status_code == 429:
                    print("    Too Many Requests (Throttled). Cooling down...")
                    time.sleep(10)
                    
        except Exception as e:
            print(f"    Failed on {endpoint}: {e}")
            last_exception = e
            time.sleep(3)
            
    raise RuntimeError(
        f"All Overpass endpoints failed. Last error: {last_exception}"
    )
                   
def fetch_pois_around_center(point, dist, tags):
    """Fetch POIs via a point and radius."""
    last_exception = None
    for endpoint in OVERPASS_ENDPOINTS:
        ox.settings.overpass_url = endpoint
        print(f"--> Attempting POI download via point/radius from: {endpoint}")
        try:
            pois = ox.features_from_point(point, dist=dist, tags=tags)
            if not pois.empty:
                return pois
            else:
                print("    Failed: Server returned 0 matching POIs.")
                last_exception = "Empty response (0 POIs found)"
        except Exception as e:
            print(f"    Failed: {e}")
            last_exception = e
            time.sleep(2)
            
    raise RuntimeError(
        f"All Overpass endpoints failed for POIs. Last error: {last_exception}"
    )
    
def ingest_v2():
    engine = create_engine(
        DB_URL, pool_pre_ping=True, pool_size=5, max_overflow=10
    )

    print(f"[1/4] Setting targeted urban center point for {PLACE_NAME}...")
    # Center point of Östersund (Latitude, Longitude)
    center_point = (63.1792, 14.6357)
    search_radius_m = 5000

    print(
        "[2/4] Extracting drive network and calculating travel times via point-radius..."
    )
    G = fetch_graph_around_center(center_point, dist=search_radius_m)
    G_proj = ox.project_graph(G, to_crs=TARGET_CRS)

    nodes_gdf, edges_gdf = ox.graph_to_gdfs(G_proj)
    # Clean nodes
    nodes_gdf = nodes_gdf.reset_index()
    nodes_gdf["node_id"] = nodes_gdf["osmid"].astype(str)
    nodes_gdf = nodes_gdf[["node_id", "geometry"]]

    # Clean edges & compute speeds/times
    edges_gdf = edges_gdf.reset_index()
    edges_gdf["source_node"] = edges_gdf["u"].astype(str)
    edges_gdf["target_node"] = edges_gdf["v"].astype(str)
    edges_gdf["edge_id"] = (
        edges_gdf["source_node"] + "_" + edges_gdf["target_node"] + "_" + edges_gdf["key"].astype(str)
    )

    speeds = []
    for _, row in edges_gdf.iterrows():
        ms = row.get("maxspeed", None)
        if isinstance(ms, list):
            ms = ms[0]
        try:
            speed = float(ms)
        except (ValueError, TypeError):
            speed = default_speed(row.get("highway", "unclassified"))
        speeds.append(speed)

    edges_gdf["speed_kph"] = speeds
    edges_gdf["length_m"] = edges_gdf["length"].astype(float)
    edges_gdf["drive_time_min"] = (
        (edges_gdf["length_m"] / 1000.0) / edges_gdf["speed_kph"]
    ) * 60.0
    edges_gdf["walk_time_min"] = ((edges_gdf["length_m"] / 1000.0) / 4.5) * 60.0

    edges_df = edges_gdf[
        [
            "edge_id",
            "source_node",
            "target_node",
            "length_m",
            "speed_kph",
            "drive_time_min",
            "walk_time_min",
        ]
    ]

    print("[3/4] Extracting disaggregated service POIs...")
    poi_tags = {
        "amenity": [
            "hospital",
            "clinic",
            "doctors",
            "pharmacy",
            "school",
            "kindergarten",
            "supermarket",
        ],
        "shop": ["supermarket", "convenience"],
    }
    pois = fetch_pois_around_center(center_point, dist=search_radius_m, tags=poi_tags)
    pois = pois.to_crs(TARGET_CRS).dropna(subset=["geometry"])
    pois["geometry"] = pois.geometry.centroid

    def categorize_poi(row):
        amenity = str(row.get("amenity", ""))
        shop = str(row.get("shop", ""))
        if amenity in ["hospital", "clinic", "doctors", "pharmacy"]:
            return "Healthcare"
        elif amenity in ["school", "kindergarten"]:
            return "Education"
        elif shop in ["supermarket", "convenience"] or amenity == "supermarket":
            return "Grocery"
        return "Other Service"

    pois["category"] = pois.apply(categorize_poi, axis=1)
    pois["name"] = pois.get("name", "Unnamed Service").fillna("Unnamed Service")
    pois_gdf = pois[["category", "name", "geometry"]].copy()

    print(f"  Extracted {len(pois_gdf)} POIs:")
    print(pois_gdf["category"].value_counts().to_string())

    print("[4/4] Ingesting V2 data layers into PostGIS...")

    # Ingest Nodes
    node_records = [
        {"node_id": str(r["node_id"]), "wkt_geom": r["geometry"].wkt}
        for _, r in nodes_gdf.iterrows()
    ]
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS ostersund_network_nodes_v2;"))
        conn.execute(
            text(
                """
            CREATE TABLE ostersund_network_nodes_v2 (
                node_id TEXT PRIMARY KEY,
                wkt_geom TEXT
            );
        """
            )
        )
        if node_records:
            conn.execute(
                text(
                    "INSERT INTO ostersund_network_nodes_v2 (node_id, wkt_geom) VALUES (:node_id, :wkt_geom);"
                ),
                node_records,
            )
        conn.execute(
            text(
                """
            ALTER TABLE ostersund_network_nodes_v2 ADD COLUMN geometry geometry(POINT, 3006);
            UPDATE ostersund_network_nodes_v2 SET geometry = ST_SetSRID(ST_GeomFromText(wkt_geom), 3006);
            ALTER TABLE ostersund_network_nodes_v2 DROP COLUMN wkt_geom;
            CREATE INDEX IF NOT EXISTS idx_v2_nodes_geom ON ostersund_network_nodes_v2 USING GIST (geometry);
        """
            )
        )

    # Ingest Edges
    edge_records = edges_df.to_dict(orient="records")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS ostersund_network_edges_v2;"))
        conn.execute(
            text(
                """
            CREATE TABLE ostersund_network_edges_v2 (
                edge_id TEXT PRIMARY KEY,
                source_node TEXT,
                target_node TEXT,
                length_m DOUBLE PRECISION,
                speed_kph DOUBLE PRECISION,
                drive_time_min DOUBLE PRECISION,
                walk_time_min DOUBLE PRECISION
            );
        """
            )
        )
        if edge_records:
            conn.execute(
                text(
                    """
                INSERT INTO ostersund_network_edges_v2 
                (edge_id, source_node, target_node, length_m, speed_kph, drive_time_min, walk_time_min)
                VALUES (:edge_id, :source_node, :target_node, :length_m, :speed_kph, :drive_time_min, :walk_time_min);
            """
                ),
                edge_records,
            )

    # Ingest POIs
    poi_records = [
        {
            "category": str(r["category"]),
            "name": str(r["name"]),
            "wkt_geom": r["geometry"].wkt,
        }
        for _, r in pois_gdf.iterrows()
    ]
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS ostersund_pois_v2;"))
        conn.execute(
            text(
                """
            CREATE TABLE ostersund_pois_v2 (
                poi_id SERIAL PRIMARY KEY,
                category TEXT,
                name TEXT,
                wkt_geom TEXT
            );
        """
            )
        )
        if poi_records:
            conn.execute(
                text(
                    "INSERT INTO ostersund_pois_v2 (category, name, wkt_geom) VALUES (:category, :name, :wkt_geom);"
                ),
                poi_records,
            )
        conn.execute(
            text(
                """
            ALTER TABLE ostersund_pois_v2 ADD COLUMN geometry geometry(POINT, 3006);
            UPDATE ostersund_pois_v2 SET geometry = ST_SetSRID(ST_GeomFromText(wkt_geom), 3006);
            ALTER TABLE ostersund_pois_v2 DROP COLUMN wkt_geom;
            CREATE INDEX IF NOT EXISTS idx_v2_pois_geom ON ostersund_pois_v2 USING GIST (geometry);
        """
            )
        )

    print("[4/4] Ingestion V2 completed successfully.")


if __name__ == "__main__":
    ingest_v2()
