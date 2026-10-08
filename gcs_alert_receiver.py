"""Penerima peringatan merokok/vape untuk laptop GCS (UDP port 5201).

Jalankan di laptop GCS:  python3 gcs_alert_receiver.py
Setiap peringatan dicetak ke layar dan disimpan ke gcs_alerts.jsonl.
"""
import json
import socket
import argparse
from datetime import datetime

ALERT_PORT = 5201


def main():
    parser = argparse.ArgumentParser(description="Penerima peringatan merokok/vape (UDP)")
    parser.add_argument("--port", type=int, default=ALERT_PORT, help="port UDP. Default: %(default)s")
    parser.add_argument("--log", default="gcs_alerts.jsonl", help="file log. Default: %(default)s")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", args.port))
    print(f"Menunggu peringatan di UDP port {args.port} ... (Ctrl+C untuk berhenti)")

    try:
        while True:
            data, addr = sock.recvfrom(65535)
            try:
                alert = json.loads(data)
            except ValueError:
                print(f"[{addr[0]}] pesan tidak valid diabaikan")
                continue
            when = datetime.fromtimestamp(alert.get("timestamp", 0)).strftime("%d-%m-%Y %H:%M:%S")
            objects = ", ".join(f"{o['class']} ({o['confidence'] * 100:.0f}%)" for o in alert.get("objects", []))
            demo = " [DEMO]" if alert.get("demo") else ""
            print("\a" + "=" * 60)
            print(f"PERINGATAN{demo}: {objects}")
            print(f"  Kamera : {alert.get('camera')} ({addr[0]})")
            print(f"  Waktu  : {when}")
            print(f"  Bukti  : {alert.get('photo')}  (folder bukti/ di perangkat kamera)")
            with open(args.log, "a", encoding="utf-8") as f:
                f.write(json.dumps(alert, ensure_ascii=False) + "\n")
    except KeyboardInterrupt:
        print("\nSelesai.")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
