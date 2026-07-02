"""
Azure Blob Storage Module for Para Athletics
=============================================
Handles Parquet file uploads/downloads to Azure Blob Storage.
Uses DuckDB for fast in-memory queries on Parquet files.

This replaces Azure SQL for better performance and lower costs.

Connection String Sources (checked in order):
1. Environment variable: AZURE_STORAGE_CONNECTION_STRING (local .env file)
2. GitHub Actions secret: AZURE_STORAGE_CONNECTION_STRING
3. Streamlit Cloud secret: AZURE_STORAGE_CONNECTION_STRING

GitHub Actions Setup:
---------------------
1. Go to your GitHub repository
2. Settings > Secrets and variables > Actions
3. Click "New repository secret"
4. Name: AZURE_STORAGE_CONNECTION_STRING
5. Value: Your Azure Blob Storage connection string

Azure Portal - Get Connection String:
--------------------------------------
1. Go to Azure Portal > Storage Accounts > paraathletics
2. Security + networking > Access keys
3. Click "Show" next to key1
4. Copy the "Connection string" value
"""

import os
import io
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Try to import Azure storage - optional for local development
try:
    from azure.storage.blob import BlobServiceClient, ContainerClient
    AZURE_AVAILABLE = True
except ImportError:
    AZURE_AVAILABLE = False
    print("Warning: azure-storage-blob not installed. Using local files only.")

# Try to import DuckDB for fast Parquet queries
try:
    import duckdb
    DUCKDB_AVAILABLE = True
except ImportError:
    DUCKDB_AVAILABLE = False
    print("Warning: duckdb not installed. Install with: pip install duckdb")


# Configuration
CONTAINER_NAME = "para-athletics-data"
BLOB_PREFIX = "parquet/"

# Local cache directory
CACHE_DIR = Path("data/parquet_cache")


def get_blob_service_client():
    """Get Azure Blob Service Client."""
    conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")

    if not conn_str:
        # Try Streamlit secrets
        try:
            import streamlit as st
            conn_str = st.secrets.get("AZURE_STORAGE_CONNECTION_STRING")
        except:
            pass

    if not conn_str:
        raise ValueError("AZURE_STORAGE_CONNECTION_STRING not found in environment or secrets")

    return BlobServiceClient.from_connection_string(conn_str)


def get_container_client():
    """Get container client, create container if it doesn't exist."""
    blob_service = get_blob_service_client()
    container_client = blob_service.get_container_client(CONTAINER_NAME)

    # Create container if it doesn't exist
    try:
        container_client.get_container_properties()
    except Exception:
        container_client.create_container()
        print(f"Created container: {CONTAINER_NAME}")

    return container_client


def upload_parquet(df: pd.DataFrame, blob_name: str) -> str:
    """
    Upload a DataFrame as Parquet to Azure Blob Storage.

    Args:
        df: DataFrame to upload
        blob_name: Name of the blob (e.g., 'results.parquet')

    Returns:
        Full blob path
    """
    if not AZURE_AVAILABLE:
        raise ImportError("azure-storage-blob not installed")

    container_client = get_container_client()
    full_blob_name = f"{BLOB_PREFIX}{blob_name}"

    # Convert DataFrame to Parquet bytes
    buffer = io.BytesIO()
    df.to_parquet(buffer, index=False, engine='pyarrow')
    buffer.seek(0)

    # Upload to blob storage
    blob_client = container_client.get_blob_client(full_blob_name)
    blob_client.upload_blob(buffer, overwrite=True)

    print(f"Uploaded {len(df):,} rows to {full_blob_name}")
    return full_blob_name


def download_parquet(blob_name: str, use_cache: bool = True) -> pd.DataFrame:
    """
    Download a Parquet file from Azure Blob Storage.

    Args:
        blob_name: Name of the blob (e.g., 'results.parquet')
        use_cache: Whether to use local cache

    Returns:
        DataFrame
    """
    full_blob_name = f"{BLOB_PREFIX}{blob_name}"
    cache_path = CACHE_DIR / blob_name

    # Check cache first
    if use_cache and cache_path.exists():
        return pd.read_parquet(cache_path)

    if not AZURE_AVAILABLE:
        raise ImportError("azure-storage-blob not installed and no cache available")

    container_client = get_container_client()
    blob_client = container_client.get_blob_client(full_blob_name)

    # Download to memory
    buffer = io.BytesIO()
    blob_data = blob_client.download_blob()
    blob_data.readinto(buffer)
    buffer.seek(0)

    df = pd.read_parquet(buffer)

    # Save to cache
    if use_cache:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_parquet(cache_path, index=False)

    print(f"Downloaded {len(df):,} rows from {full_blob_name}")
    return df


def get_parquet_url(blob_name: str) -> str:
    """
    Get the public URL for a Parquet blob.
    Used by DuckDB for direct remote queries.

    Args:
        blob_name: Name of the blob

    Returns:
        Public URL string
    """
    conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")

    # Parse account name from connection string
    parts = dict(part.split("=", 1) for part in conn_str.split(";") if "=" in part)
    account_name = parts.get("AccountName", "paraathletics")

    return f"https://{account_name}.blob.core.windows.net/{CONTAINER_NAME}/{BLOB_PREFIX}{blob_name}"


def list_parquet_files() -> list:
    """List all Parquet files in the container."""
    if not AZURE_AVAILABLE:
        # Return local cache files
        if CACHE_DIR.exists():
            return [f.name for f in CACHE_DIR.glob("*.parquet")]
        return []

    container_client = get_container_client()
    blobs = container_client.list_blobs(name_starts_with=BLOB_PREFIX)
    return [b.name.replace(BLOB_PREFIX, "") for b in blobs if b.name.endswith(".parquet")]


def query_parquet(sql: str, blob_name: str = None, local_path: str = None) -> pd.DataFrame:
    """
    Query a Parquet file using DuckDB SQL.

    Args:
        sql: SQL query (use 'data' as table name)
        blob_name: Name of blob to query (optional)
        local_path: Path to local Parquet file (optional)

    Returns:
        Query result as DataFrame

    Example:
        query_parquet("SELECT * FROM data WHERE nationality = 'KSA' LIMIT 100",
                      blob_name="results.parquet")
    """
    if not DUCKDB_AVAILABLE:
        raise ImportError("duckdb not installed. Install with: pip install duckdb")

    # Determine data source
    if local_path:
        source = local_path
    elif blob_name:
        cache_path = CACHE_DIR / blob_name
        if cache_path.exists():
            source = str(cache_path)
        else:
            # Download first
            download_parquet(blob_name, use_cache=True)
            source = str(cache_path)
    else:
        raise ValueError("Must provide blob_name or local_path")

    # Create DuckDB connection and query
    conn = duckdb.connect(":memory:")

    # Read Parquet and create 'data' table
    conn.execute(f"CREATE TABLE data AS SELECT * FROM read_parquet('{source}')")

    # Execute query
    result = conn.execute(sql).fetchdf()
    conn.close()

    return result


def test_connection() -> dict:
    """Test Azure Blob Storage connection."""
    result = {
        "azure_available": AZURE_AVAILABLE,
        "duckdb_available": DUCKDB_AVAILABLE,
        "connection_string_found": bool(os.getenv("AZURE_STORAGE_CONNECTION_STRING")),
        "container_name": CONTAINER_NAME,
        "status": "unknown"
    }

    if not AZURE_AVAILABLE:
        result["status"] = "azure-storage-blob not installed"
        return result

    if not result["connection_string_found"]:
        result["status"] = "no connection string"
        return result

    try:
        container_client = get_container_client()
        blobs = list(container_client.list_blobs(name_starts_with=BLOB_PREFIX))
        result["parquet_files"] = len([b for b in blobs if b.name.endswith(".parquet")])
        result["status"] = "connected"
    except Exception as e:
        result["status"] = f"error: {str(e)}"

    return result


if __name__ == "__main__":
    print("Testing Azure Blob Storage connection...")
    result = test_connection()
    for key, value in result.items():
        print(f"  {key}: {value}")
