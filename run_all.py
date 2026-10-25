import subprocess
import time

def run_script(script_name):
    return subprocess.Popen(["python", script_name])

if __name__ == "__main__":
    print("🚀 Starting multi-script background runner...")

    # Launch both scripts concurrently
    bot_process = run_script("bot.py")
    test_process = run_script("test.py")

    try:
        while True:
            # Monitor processes and restart them if they ever crash
            if bot_process.poll() is not None:
                print("⚠️ bot.py stopped. Restarting...")
                bot_process = run_script("bot.py")

            if test_process.poll() is not None:
                print("⚠️ test.py stopped. Restarting...")
                test_process = run_script("test.py")

            time.sleep(10)
    except KeyboardInterrupt:
        bot_process.terminate()
        test_process.terminate()