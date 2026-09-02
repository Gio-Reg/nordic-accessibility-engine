import pandas as pd
import numpy as np
import networkx as nx
from scipy.spatial import cKDTree
from sqlalchemy import create_engine, text

# Configuration
DB_URL = "postgresql://postgres:postgres@localhost:5432/ostersund_accessibility"
WALK_SPEED_KMH = 4.5  # Standard walking speed

def run_accessibility_pipeline():
    engine = create_engine(DB_URL, pool_pre_ping=True)
    
    print("[1/4] Loading nodes, edges, and POIs from PostGIS...")
    # 1. Load data from PostgreSQL using a raw connection
    raw_conn = engine.raw_connection()
    try:
        nodes_df = pd.read_sql_query(
            "SELECT node_id, ST_X(geometry) as x, ST_Y(geometry) as y FROM ostersund_network_nodes_v2;",
            raw_conn
        )
        edges_df = pd.read_sql_query(
            "SELECT source_node, target_node, walk_time_min FROM ostersund_network_edges_v2;",
            raw_conn
        )
        pois_df = pd.read_sql_query(
            "SELECT poi_id, category, ST_X(geometry) as x, ST_Y(geometry) as y FROM ostersund_pois_v2;",
            raw_conn
        )
    finally:
        raw_conn.close()
    
    # 2. Build NetworkX Graph for Routing
    print("[2/4] Constructing network graph...")
    G = nx.DiGraph()
    for _, row in edges_df.iterrows():
        G.add_edge(row["source_node"], row["target_node"], weight=float(row["walk_time_min"]))

    # 3. Snap POIs to Nearest Network Nodes using KD-Tree
    print("[3/4] Snapping POIs to nearest network nodes...")
    node_coords = nodes_df[["x", "y"]].to_numpy()
    tree = cKDTree(node_coords)
    
    poi_coords = pois_df[["x", "y"]].to_numpy()
    _, nearest_indices = tree.query(poi_coords)
    pois_df["nearest_node_id"] = nodes_df.iloc[nearest_indices]["node_id"].values

    # 4. Multi-Source Dijkstra for Each Category
    print("[4/4] Computing shortest walking paths & 15-minute accessibility scores...")
    categories = ["Grocery", "Education", "Healthcare"]
    scores_df = nodes_df[["node_id"]].copy()

    for cat in categories:
        cat_pois = pois_df[pois_df["category"] == cat]
        target_nodes = set(cat_pois["nearest_node_id"].tolist())
        
        # Multi-source Dijkstra (reversed graph to compute source -> target)
        # Reversing graph allows computing distance FROM all nodes TO target POIs efficiently
        distances = nx.multi_source_dijkstra_path_length(G.reverse(copy=False), sources=target_nodes)
        
        scores_df[f"walk_time_{cat.lower()}_min"] = scores_df["node_id"].map(distances)

    # Fill unreached nodes with infinity or a high threshold
    scores_df = scores_df.fillna(999.0)

    # Composite 15-Minute City Metrics
    scores_df["max_essential_walk_min"] = scores_df[
        ["walk_time_grocery_min", "walk_time_education_min", "walk_time_healthcare_min"]
    ].max(axis=1)

    scores_df["is_15min_city"] = scores_df["max_essential_walk_min"] <= 15.0

    # Write Results to PostGIS
    print("Writing accessibility scores back to database...")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS ostersund_accessibility_scores_v2;"))
        conn.execute(text("""
            CREATE TABLE ostersund_accessibility_scores_v2 (
                node_id TEXT PRIMARY KEY,
                walk_time_grocery_min DOUBLE PRECISION,
                walk_time_education_min DOUBLE PRECISION,
                walk_time_healthcare_min DOUBLE PRECISION,
                max_essential_walk_min DOUBLE PRECISION,
                is_15min_city BOOLEAN
            );
        """))
        
        # Bulk insert directly via SQLAlchemy parameter binding
        records = scores_df.to_dict(orient="records")
        conn.execute(
            text("""
                INSERT INTO ostersund_accessibility_scores_v2 (
                    node_id, 
                    walk_time_grocery_min, 
                    walk_time_education_min, 
                    walk_time_healthcare_min, 
                    max_essential_walk_min, 
                    is_15min_city
                ) VALUES (
                    :node_id, 
                    :walk_time_grocery_min, 
                    :walk_time_education_min, 
                    :walk_time_healthcare_min, 
                    :max_essential_walk_min, 
                    :is_15min_city
                );
            """),
            records
        )
        
    # Calculate Summary Statistics
    pct_15min = (scores_df["is_15min_city"].sum() / len(scores_df)) * 100
    print(f"\n Pipeline Completed Successfully!")
    print(f"Total Nodes Analyzed: {len(scores_df):,}")
    print(f"15-Minute City Coverage: {pct_15min:.1f}% of network nodes have access to ALL 3 service types within 15 mins.")
    
if __name__ == "__main__":
    run_accessibility_pipeline()
