FROM ctfd/ctfd:latest

COPY territory_control/requirements.txt /tmp/territory-control-requirements.txt
RUN pip install --no-cache-dir -r /tmp/territory-control-requirements.txt
COPY territory_control /opt/CTFd/CTFd/plugins/territory_control
