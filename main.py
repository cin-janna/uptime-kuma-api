
from kubernetes import client, config as k8s_config
from uptime_kuma_api import UptimeKumaApi
import sqlite3
import time
from decouple import config


API_URL = config("API_URL", default=None)
USERNAME = config("USERNAME", default=None)
PASSWORD = config("PASSWORD", default=None)
KUMA_NAMESPACE = config("KUMA_NAMESPACE", default=None)
DB_PATH = config("DB_PATH", default=None)

def load_k8s_config():
    k8s_config.load_incluster_config()

def safe_get_status_page(kuma, slug):
    try:
        page = kuma.get_status_page(slug)
        return page
    except Exception as e:
        print(f"[ERROR] get_status_page {slug}: {e}")
        return None

def load_kuma_entities(k8s_api):
    group = "autokuma.bigboot.dev"
    version = "v1"
    plural = "kumaentities"
    namespace = KUMA_NAMESPACE

    resp = k8s_api.list_namespaced_custom_object(
        group=group,
        version=version,
        namespace=namespace,
        plural=plural,
    )
    return resp.get("items", [])


def load_monitors_from_db(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM monitor")
    return {name: mid for mid, name in cursor.fetchall()}


def ensure_status_page(kuma, slug, title, existing_pages):
    page = existing_pages.get(slug)

    if not page:
        print(f"[+] Creating status page: {slug}")
        kuma.add_status_page(slug=slug, title=title)

        time.sleep(1)

        existing_pages[slug] = {"slug": slug}

    return existing_pages[slug]

def sync_monitors_to_page(kuma, slug, monitor_ids):
    page_data = safe_get_status_page(kuma, slug)
    if not page_data:
        print(f"[!] Page not found after create: {slug}")
        return

    public_group_list = page_data.get("publicGroupList", [])
    if not public_group_list:
        public_group_list = [{
            "name": slug,
            "weight": 1,
            "monitorList": []
        }]

    group = public_group_list[0]
    monitor_list = group.get("monitorList", [])
    existing_ids = {int(m["id"]) for m in monitor_list}

    added = False
    for monitor_id in set(monitor_ids):
        if int(monitor_id) not in existing_ids:
            monitor_list.append({"id": int(monitor_id)})
            existing_ids.add(int(monitor_id))
            print(f"[+] Link monitor {monitor_id} → {slug}")
            added = True

    if not added:
        return

    print(f"[+] Link monitor {monitor_id} → {slug}")

    payload = {
        "slug": slug,
        "icon": "/icon.svg",
        "theme": "auto",
        "published": True,
        "showTags": True,
        "customCSS": "body {\n}\n",
        "showPoweredBy": True,
        "showCertificateExpiry": True,
        "publicGroupList": [{
            "name": "Services",
            "weight": group.get("weight", 1),
            "monitorList": monitor_list
        }]
    }

    kuma.save_status_page(**payload)


def cleanup_orphan_pages(kuma, existing_pages, desired_pages):
    print("[*] Cleanup status pages")

    for slug, page in existing_pages.items():
        if slug not in desired_pages:
            print(f"[-] Delete orphan page: {slug}")

def run_once():
    k8s_api = client.CustomObjectsApi()
    conn = sqlite3.connect(DB_PATH, timeout=10)

    kuma = UptimeKumaApi(API_URL)
    kuma.login(USERNAME, PASSWORD)

    kuma_entities = load_kuma_entities(k8s_api)
    monitors = load_monitors_from_db(conn)

    existing_pages = {
        p["slug"]: p for p in kuma.get_status_pages()
    }

    desired_pages = set()

    group_entities = {}
    http_entities  = []

    for item in kuma_entities:
        config_spec = item.get("spec", {}).get("config", {})
        name  = config_spec.get("name") or config_spec.get("Name")
        type = config_spec.get("type") or config_spec.get("Type")

        if not name:
            continue

        if type == "group":
            group_entities[name] = name
        elif type == "http":
            parent_name = config_spec.get("parent_name")
            http_entities.append((name, parent_name))

    pages_to_monitors = {}
    print(f"[DEBUG] pages_to_monitors: {pages_to_monitors}")

    for monitor_name, parent_name in http_entities:
        monitor_id = monitors.get(monitor_name)
        print(f"[DEBUG] monitor={monitor_name} parent={parent_name}")
        if not monitor_id:
            print(f"[!] Monitor not found in DB: {monitor_name}")
            continue

        if not parent_name:
            print(f"[!] No parent_name for monitor: {monitor_name}")
            continue

        if parent_name and parent_name not in group_entities:
            print(f"[!] Group not found in entities: {parent_name}")

        if parent_name not in pages_to_monitors:
            pages_to_monitors[parent_name] = []

        pages_to_monitors[parent_name].append(monitor_id)

    for parent_name, monitor_ids in pages_to_monitors.items():
        slug  = parent_name.split("-")[0]
        title = parent_name
        desired_pages.add(slug)
        ensure_status_page(kuma, slug, title, existing_pages)

        sync_monitors_to_page(kuma, slug, monitor_ids)

    cleanup_orphan_pages(kuma, existing_pages, desired_pages)

    conn.close()

if __name__ == "__main__":
    load_k8s_config()

    while True:
        try:
            run_once()
        except Exception as e:
            print(f"[ERROR] {e}")

        time.sleep(60)
