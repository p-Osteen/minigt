import os
import sys
import threading
import webbrowser
import socketserver
from http.server import SimpleHTTPRequestHandler

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from database.db_manager import init_db, clear_all_data, clear_brand_data
from crawler.crawler import MINI_GTCrawler

PORT = 8000
server = None
server_thread = None


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True


def start_preview_server():
    global server, server_thread
    if server:
        print(f"\n[INFO] Preview server already running at http://localhost:{PORT}")
        webbrowser.open(f"http://localhost:{PORT}")
        return
    try:
        server = ThreadedTCPServer(("127.0.0.1", PORT), SimpleHTTPRequestHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        print(f"\n[SUCCESS] Preview server started at http://localhost:{PORT}")
        webbrowser.open(f"http://localhost:{PORT}")
    except Exception as e:
        print(f"\n[ERROR] Failed to start server: {e}")


def stop_preview_server():
    global server, server_thread
    if server:
        server.shutdown()
        server.server_close()
        server = None
        server_thread = None
        print("\n[INFO] Preview server stopped.")
    else:
        print("\n[INFO] Preview server is not running.")




def main_menu():
    init_db()

    while True:
        print("\n======================================")
        print("   MINI GT 1:64 Scale Catalog Panel   ")
        print("======================================")
        print("  1. Scrape / Update Catalog")
        print("  2. Resume Interrupted Scrape")
        print("  3. Deploy to GitHub Pages")
        if server:
            print(f"  4. Stop Preview Server  (http://localhost:{PORT})")
        else:
            print("  4. Start Preview Server")
        print("  5. Clear Catalog Data")
        print("  6. Exit")
        print("======================================")

        choice = input("Enter option (1-6): ").strip()

        if choice == "1":
            print("\nSelect brand to scrape:")
            print("  1. MINI GT")
            print("  2. Hot Wheels")
            print("  3. Pop Race")
            print("  4. All")
            brand_choice = input("Enter choice (1-4): ").strip()
            brand_map = {"1": "MINI GT", "2": "Hot Wheels", "3": "Pop Race", "4": "All"}
            brand_name = brand_map.get(brand_choice)
            if brand_name:
                target_brand = None if brand_name == "All" else brand_name
                print(f"\n[INFO] Starting fresh discovery & crawl for {brand_name}...")
                try:
                    crawler = MINI_GTCrawler()
                    crawler.run_discovery(target_brand)
                    crawler.run_crawler(target_brand)
                    print("\n[INFO] Auto-deploying to GitHub Pages...")
                    from deploy import deploy
                    deploy()
                except KeyboardInterrupt:
                    print("\n[INFO] Crawl interrupted. State cached.")
            else:
                print("\n[ERROR] Invalid choice.")

        elif choice == "2":
            print("\n[INFO] Resuming crawl from saved state...")
            try:
                crawler = MINI_GTCrawler()
                crawler.run_crawler()
            except KeyboardInterrupt:
                print("\n[INFO] Crawl interrupted. State cached.")

        elif choice == "3":
            print("\n[INFO] Starting GitHub Pages deployment...")
            from deploy import deploy
            deploy()

        elif choice == "4":
            if server:
                stop_preview_server()
            else:
                start_preview_server()

        elif choice == "5":
            print("\nSelect brand to clear:")
            print("  1. MINI GT")
            print("  2. Hot Wheels")
            print("  3. Pop Race")
            print("  4. All (Complete Reset)")
            brand_choice = input("Enter choice (1-4): ").strip()
            brand_map = {"1": "MINI GT", "2": "Hot Wheels", "3": "Pop Race", "4": "All"}
            brand_name = brand_map.get(brand_choice)
            if brand_name:
                if brand_name == "All":
                    confirm = input(
                        "\n[WARNING] This deletes all database records, caches, and logs. Type 'yes' to proceed: "
                    ).strip().lower()
                    if confirm == "yes":
                        clear_all_data()
                else:
                    confirm = input(
                        f"\n[WARNING] This deletes all database and JSON records for {brand_name}. Type 'yes' to proceed: "
                    ).strip().lower()
                    if confirm == "yes":
                        clear_brand_data(brand_name)
            else:
                print("\n[ERROR] Invalid choice.")

        elif choice == "6":
            stop_preview_server()
            print("\nGoodbye!")
            break

        else:
            print("\n[ERROR] Invalid option.")


if __name__ == "__main__":
    try:
        main_menu()
    except KeyboardInterrupt:
        stop_preview_server()
        print("\nGoodbye!")
        sys.exit(0)
