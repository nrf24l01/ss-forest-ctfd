import time

from CTFd import create_app

from .instance_manager import expire_instances


def main():
    app = create_app()
    while True:
        with app.app_context():
            expire_instances()
        time.sleep(60)


if __name__ == "__main__":
    main()
