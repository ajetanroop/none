#!/usr/bin/env python3

import paramiko
import threading
import time
import os
import re
import sys
import signal
import socket

print_lock = threading.Lock()

GREEN = "\033[32m"
RED = "\033[31m"
BLUE = "\033[34m"
YELLOW = "\033[33m"
RESET = "\033[0m"

def load_ssh_config():
    ssh_config_file = os.path.expanduser("~/.ssh/config")
    config = paramiko.SSHConfig()
    if os.path.exists(ssh_config_file):
        with open(ssh_config_file, 'r') as f:
            config.parse(f)
    else:
        raise FileNotFoundError("SSH config file not found at ~/.ssh/config")
    return config

def is_localhost(hostname):
    """Check if the given hostname refers to the local machine."""
    try:
        # Get hostname from the system (like running `hostname` in the shell)
        system_hostname = subprocess.check_output(["hostname"], text=True).strip()

        # Common loopback names and addresses
        local_names = {
            "localhost",
            "127.0.0.1",
            "::1",
            system_hostname,
            socket.gethostname(),
            socket.getfqdn(),
            socket.gethostbyname(socket.gethostname()),
        }

        # Add all IPs assigned to local host
        try:
            local_ips = socket.gethostbyname_ex(socket.gethostname())[2]
            local_names.update(local_ips)
        except socket.gaierror:
            pass  # ignore if can't resolve IPs

        return hostname in local_names
    except Exception:
        return False

def connect_to_host(hostname):
    if is_localhost(hostname):
        return None  # Signal that we're running locally

    config = load_ssh_config()
    host_config = config.lookup(hostname)
    if not host_config:
        raise ValueError(f"No configuration found for {hostname}")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs = {
        "hostname": host_config.get("hostname", hostname),
        "username": host_config.get("user"),
        "port": int(host_config.get("port", 22)),
    }

    if "proxyjump" in host_config:
        proxy_host = host_config["proxyjump"]
        proxy_config = config.lookup(proxy_host)
        if not proxy_config:
            raise ValueError(f"No configuration found for proxy jump host: {proxy_host}")

        proxy_client = paramiko.SSHClient()
        proxy_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        proxy_client.connect(
            hostname=proxy_config.get("hostname", proxy_host),
            username=proxy_config.get("user"),
            port=int(proxy_config.get("port", 22)),
        )
        proxy_transport = proxy_client.get_transport()
        dest_addr = (connect_kwargs["hostname"], connect_kwargs["port"])
        local_addr = ("127.0.0.1", 0)
        proxy_channel = proxy_transport.open_channel("direct-tcpip", dest_addr, local_addr)
        connect_kwargs["sock"] = proxy_channel

    client.connect(**connect_kwargs)
    return client

def run_command_with_timeout(client, command, timeout, hostname="unknown"):
    try:
        if client is None:
            # Run locally
            proc = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                out, err = proc.communicate(timeout=timeout)
                return True, out.decode().strip() if out else err.decode().strip()
            except subprocess.TimeoutExpired:
                proc.kill()
                with print_lock:
                    print(f"{RED}[{hostname}] Timeout exceeded while running: {command}{RESET}")
                return False, f"Timeout exceeded while running: {command}"
        else:
            # Run via SSH
            stdin, stdout, stderr = client.exec_command(command)
            start = time.time()
            while not stdout.channel.exit_status_ready():
                if time.time() - start > timeout:
                    stdout.channel.close()
                    with print_lock:
                        print(f"{RED}[{hostname}] Timeout exceeded while running: {command}{RESET}")
                    return False, f"Timeout exceeded while running: {command}"
                time.sleep(0.5)
            out = stdout.read().decode().strip()
            err = stderr.read().decode().strip()
            return True, out if out else err
    except Exception as e:
        with print_lock:
            print(f"{RED}[{hostname}] Exception during command: {command}\n{e}{RESET}")
        return False, str(e)

def check_and_start_service(client, service, hostname, verbose=True):
    success, output = run_command_with_timeout(client, f"systemctl is-active {service}", 5, hostname=hostname)
    if success and "active" in output:
        if verbose:
            with print_lock:
                print(f"{GREEN}[{hostname}] {service} is already running.{RESET}")
        return
    if verbose:
        with print_lock:
            print(f"{YELLOW}[{hostname}] {service} is not running. Attempting to start...{RESET}")
    success, output = run_command_with_timeout(client, f"sudo systemctl start {service}", 10, hostname=hostname)
    if success:
        with print_lock:
            print(f"{GREEN}[{hostname}] Started {service} successfully.{RESET}")
    else:
        with print_lock:
            print(f"{RED}[{hostname}] Failed to start {service}: {output}{RESET}")

def signal_handler(sig, frame):
    print("\nCtrl-C received. Exiting now!")
    sys.exit(1)


def monitor_log_file(
    hostname: str,
    filepath: str,
    timeout: int,
    keyword_expr: str,
    truncate_file: bool = False,
    print_output: bool = True,
    result_dict: dict = None,
):
    with print_lock:
        print(f"{BLUE}[{hostname}] Starting monitor_log_file thread (watching {filepath}){RESET}")

    client = connect_to_host(hostname)

    if truncate_file:
        with print_lock:
            print(f"{YELLOW}[{hostname}] Truncating {filepath}{RESET}")
        run_command_with_timeout(client, f"sudo truncate -s 0 {filepath}", 5, hostname=hostname)

    keywords = set(re.findall(r"'(.*?)'", keyword_expr))
    seen = set()

    # Step 1: Check existing log content using `cat`
    with print_lock:
        print(f"{YELLOW}[{hostname}] Checking existing file content with cat{RESET}")
    stdin, stdout, stderr = client.exec_command(f"sudo cat {filepath}")
    file_content = stdout.read().decode('utf-8', errors='ignore')

    for line in file_content.splitlines():
        line = line.strip()
        for kw in keywords:
            if kw in line:
                seen.add(kw)
                with print_lock:
                    print(f"{GREEN}[{hostname}] Matched keyword in existing content: '{kw}'{RESET}")
        if print_output and line:
            with print_lock:
                print(f"[{hostname}] LOG (cat): {line}")

    # If all matched already, no need to tail
    if seen == keywords:
        with print_lock:
            print(f"{GREEN}[{hostname}] All keywords matched in existing content! Skipping tail.{RESET}")
        if result_dict is not None:
            result_dict[threading.current_thread().name] = len(seen)
        client.close()
        return

    # Step 2: Start tail -F for live monitoring
    command = f"timeout {timeout}s sudo tail -F {filepath}"
    with print_lock:
        print(f"{YELLOW}[{hostname}] Executing command: {command}{RESET}")
    stdin, stdout, stderr = client.exec_command(command)

    stdout.channel.settimeout(1.0)
    start_time = time.time()
    progress_reported = set()

    try:
        while time.time() - start_time < timeout:
            elapsed = int(time.time() - start_time)
            step = elapsed // 10
            if step not in progress_reported:
                progress_reported.add(step)
                remaining = timeout - elapsed
                with print_lock:
                    print(f"{YELLOW}[{hostname}] {remaining} seconds remaining{RESET}")

            try:
                line = stdout.readline()
            except socket.timeout:
                continue

            if not line:
                time.sleep(0.1)
                continue
            line = line.strip()

            for kw in keywords:
                if kw in line and kw not in seen:
                    seen.add(kw)
                    with print_lock:
                        print(f"{GREEN}[{hostname}] Matched keyword: '{kw}'{RESET}")

            if print_output:
                with print_lock:
                    print(f"[{hostname}] LOG: {line}")

            if seen == keywords:
                with print_lock:
                    print(f"{GREEN}[{hostname}] All keywords matched! Exiting early.{RESET}")
                break

        if print_output:
            with print_lock:
                print(f"{BLUE}[{hostname}] Monitoring finished for {filepath}.{RESET}")

    except Exception as e:
        with print_lock:
            print(f"{RED}[{hostname}] Exception in monitor_log_file: {e}{RESET}")
    finally:
        stdout.channel.close()
        client.close()
        with print_lock:
            print(f"{BLUE}[{hostname}] monitor_log_file thread finished (filepath: {filepath}){RESET}")
        if result_dict is not None:
            result_dict[threading.current_thread().name] = len(seen)

def build_and_run_client(
    hostname: str,
    command: str,
    check_stuck: bool = True,
    stuck_check_interval: int = 5,
    stuck_checks: int = 6,
    program_name: str = None,
    working_dir: str = "/opt/MasterThesis/trafGen",
):
    with print_lock:
        print(f"{BLUE}[{hostname}] build_and_run_client thread started (running '{command}') {RESET}")

    client = connect_to_host(hostname)
    program = program_name if program_name else command.split()[0].split("./")[-1]
    log_file = f"/tmp/{program}.log"
    pid_file = f"/tmp/{program}.pid"
    full_cmd = f"cd {working_dir} && nohup {command} > {log_file} 2>&1 & echo $! > {pid_file}"

    run_command_with_timeout(client, full_cmd, 5, hostname=hostname)
    status, output = run_command_with_timeout(client, f"cat {pid_file}", 5, hostname=hostname)
    pid = output.strip() if status else None

    last_lines = []
    same_count = 0

    start_time = time.time()
    progress_reported = set()
    while time.time() - start_time < 2 * 60:
        if not pid:
            break

        ps_status, ps_output = run_command_with_timeout(client, f"ps -p {pid}", 5, hostname=hostname)
        is_running = ps_output and (re.search(rf"\b{pid}\b", ps_output) is not None)

        if not is_running:
            with print_lock:
                print(f"{GREEN}[{hostname}] {program} completed successfully.{RESET}")
            success, final_out = run_command_with_timeout(client, f"cat {log_file}", 5, hostname=hostname)
            if success:
                with print_lock:
                    print(f"{YELLOW}[{hostname}] Final output from {program}:{RESET}\n{final_out}")
            break

        if check_stuck:
            out_status, out = run_command_with_timeout(client, f"tail -n 5 {log_file}", 5, hostname=hostname)
            if out_status:
                lines = out.splitlines()
                if lines == last_lines:
                    same_count += 1
                    if same_count >= stuck_checks:
                        with print_lock:
                            print(f"{RED}[{hostname}] {program} appears stuck. Killing it.{RESET}")
                        run_command_with_timeout(client, f"sudo kill {pid}", 5, hostname=hostname)
                        success, final_out = run_command_with_timeout(client, f"cat {log_file}", 5, hostname=hostname)
                        if success:
                            with print_lock:
                                print(f"{YELLOW}[{hostname}] Final output from {program}:{RESET}\n{final_out}")
                        break
                else:
                    last_lines = lines
                    same_count = 0

        elapsed = int(time.time() - start_time)
        step = elapsed // 10
        if step not in progress_reported:
            progress_reported.add(step)
            with print_lock:
                print(f"{YELLOW}[{hostname}] build_and_run_client progress: {elapsed} seconds elapsed{RESET}")

        time.sleep(stuck_check_interval)

    client.close()
    with print_lock:
        print(f"{BLUE}[{hostname}] build_and_run_client thread finished{RESET}")

def flush_conntrack_on_hosts(hosts):
    def flush_conntrack(host):
        with print_lock:
            print(f"{BLUE}[{host}] Flushing conntrack table...{RESET}")
        client = connect_to_host(host)
        run_command_with_timeout(client, "sudo conntrack -F", 5, hostname=host)
        client.close()
        with print_lock:
            print(f"{GREEN}[{host}] Conntrack table flushed.{RESET}")

    threads = []
    for h in hosts:
        t = threading.Thread(target=flush_conntrack, args=(h,), name=f"FlushConntrack-{h}")
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    with print_lock:
        print(f"{GREEN}All conntrack tables flushed.{RESET}")

def run_command_with_timeout_verbose(client, command, timeout, hostname="unknown"):
    try:
        stdin, stdout, stderr = client.exec_command(command)
        start = time.time()
        while not stdout.channel.exit_status_ready():
            if time.time() - start > timeout:
                stdout.channel.close()
                with print_lock:
                    print(f"{RED}[{hostname}] Timeout exceeded while running: {command}{RESET}")
                return False, "", f"Timeout exceeded while running: {command}"
            time.sleep(0.5)

        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()

        if out:
            print(f"{YELLOW}[{hostname}] STDOUT:\n{out}{RESET}")
        if err:
            print(f"{RED}[{hostname}] STDERR:\n{err}{RESET}")

        return True, out, err

    except Exception as e:
        with print_lock:
            print(f"{RED}[{hostname}] Exception during command: {command}\n{e}{RESET}")
        return False, "", str(e)


def check():
    log_results = {}

    # Phase 1: Check and start services
    service_map = {
        "convsrc5": ["tcp_server.service", "udp_server.service"],
        "convsrc8": ["tcp_server.service", "udp_server.service"],
        "connt1": ["ptpd2.service", "conntrack_logger.service"],
        "connt2": ["ptpd2.service", "conntrack_logger.service"],
    }
    signal.signal(signal.SIGINT, signal_handler)
    threads = []
    for host, services in service_map.items():
        def check_services(hostname, services):
            with print_lock:
                print(f"{BLUE}[{hostname}] Service check thread started for {services}{RESET}")
            client = connect_to_host(hostname)
            for s in services:
                check_and_start_service(client, s, hostname)
            client.close()
            with print_lock:
                print(f"{BLUE}[{hostname}] Service check thread finished{RESET}")
        t = threading.Thread(target=check_services, args=(host, services), name=f"ServiceCheck-{host}")
        threads.append(t)
        with print_lock:
            print(f"{YELLOW}Starting thread: ServiceCheck-{host} for {services}{RESET}")
        t.start()
    for t in threads:
        with print_lock:
            print(f"{YELLOW}Joining thread: {t.name}{RESET}")
        t.join()

    # Phase 2: Flush conntrack on required hosts
    conntrack_hosts = ["connt1", "connt2"]
    flush_conntrack_on_hosts(conntrack_hosts)

    # Wait 5 seconds before proceeding
    with print_lock:
        print(f"{YELLOW}Waiting 5 seconds after conntrack flush...{RESET}")
    time.sleep(5)

    # Monitor logs in parallel before running clients
    log_threads = [
        threading.Thread(
            target=monitor_log_file,
            kwargs=dict(
                hostname="convsrc2",
                filepath="/var/log/conntrack.log",
                timeout=60,
                keyword_expr="'connt1' AND 'connt2'",
                truncate_file=True,
                print_output=True,
                result_dict=log_results
            ),
            name="Monitor-conntrack"
        ),
        threading.Thread(
            target=monitor_log_file,
            kwargs=dict(
                hostname="convsrc2",
                filepath="/var/log/ptp.log",
                timeout=60,
                keyword_expr="'connt1' AND 'connt2'",
                truncate_file=True,
                print_output=True,
                result_dict=log_results
            ),
            name="Monitor-ptp"
        )
    ]
    for lt in log_threads:
        with print_lock:
            print(f"{YELLOW}Starting thread: {lt.name}{RESET}")
        lt.start()

    # Run TCP/UDP clients in parallel
    client_threads = [
        threading.Thread(
            target=build_and_run_client,
            kwargs=dict(
                hostname="convsrc1",
                command="sudo ./tcp_client_er -s 172.16.1.1 -p 2000 -n 10 -c 1 -w 1 -a 172.16.1.10-22 -k -r 10000-65000 -t 1"
            ),
            name="Client-tcp-convsrc1"
        ),
        threading.Thread(
            target=build_and_run_client,
            kwargs=dict(
                hostname="convsrc1",
                command="./udp_client_sub -s 172.16.1.1 -p 3000 -n 10 -c 1 -a 172.16.1.10-22 -r 10000-65000"
            ),
            name="Client-udp-convsrc1"
        ),
        threading.Thread(
            target=build_and_run_client,
            kwargs=dict(
                hostname="convsrc2",
                command="./udp_client_sub -s 172.16.1.1 -p 3000 -n 10 -c 1 -a 172.16.1.26-39 -r 10000-65000"
            ),
            name="Client-udp-convsrc2"
        ),
        threading.Thread(
            target=build_and_run_client,
            kwargs=dict(
                hostname="convsrc2",
                command="sudo ./tcp_client_er -s 172.16.1.1 -p 2000 -n 10 -c 1 -w 1 -a 172.16.1.26-39 -k -r 10000-65000 -t 1"
            ),
            name="Client-tcp-convsrc2"
        )
    ]
    for ct in client_threads:
        with print_lock:
            print(f"{YELLOW}Starting thread: {ct.name}{RESET}")
        ct.start()
    for ct in client_threads:
        with print_lock:
            print(f"{YELLOW}Joining thread: {ct.name}{RESET}")
        ct.join()

    # Join the log monitoring threads and collect results
    matches_found = 0
    for lt in log_threads:
        with print_lock:
            print(f"{YELLOW}Joining thread: {lt.name}{RESET}")
        lt.join()
        matches_found += log_results.get(lt.name, 0)

    with print_lock:
        print(f"\n{GREEN}All processes complete. Matches found: {matches_found}{RESET}")

    return matches_found

def monitor_file_growth(hostname, filepath, stop_event, check_interval=10, label=None):
    label = label or filepath
    prev_line_count = None

    client = connect_to_host(hostname)

    while not stop_event.is_set():
        status, output = run_command_with_timeout(client, f"wc -l {filepath}", 5, hostname=hostname)
        if status:
            try:
                current_count = int(output.strip().split()[0])
                if prev_line_count is not None and current_count <= prev_line_count:
                    print(f"{RED}[{label}] Log is NOT growing! ({prev_line_count} → {current_count}){RESET}")
                else:
                    print(f"{GREEN}[{label}] Log growing: {prev_line_count} → {current_count}{RESET}")
                prev_line_count = current_count
            except Exception as e:
                print(f"{RED}[{label}] Failed to parse wc output: {output.strip()} - {e}{RESET}")
        else:
            print(f"{RED}[{label}] Failed to check log file on {hostname}.{RESET}")

        time.sleep(check_interval)

    client.close()



def run_experimentation(repeats, experimentation_name):
    for i in range(repeats):
        print(f"{GREEN}=== Starting Experimentation Iteration {i+1}/{repeats} ==={RESET}")

        # Step 1: Flush conntrack on target hosts
        conntrack_hosts = ["connt1", "connt2"]
        flush_conntrack_on_hosts(conntrack_hosts)

        # Step 2: Define log files to monitor during the client run
        log_files_to_monitor = {
            "convsrc2": [f"/var/log/exp/{experimentation_name}_ca.csv"],
            "connt1": [
                f"/var/log/exp/{experimentation_name}_conntrackd_n_monitor.csv",
                f"/var/log/exp/{experimentation_name}_conntrackd_cm_monitor.csv"
            ]
        }

        # Step 3: Start log monitoring threads
        stop_event = threading.Event()
        monitor_threads = []
        for host, files in log_files_to_monitor.items():
            for fpath in files:
                t = threading.Thread(
                    target=monitor_file_growth,
                    kwargs={
                        "hostname": host,
                        "filepath": fpath,
                        "stop_event": stop_event,
                        "check_interval": 10,
                        "label": f"{host}:{os.path.basename(fpath)}"
                    },
                    name=f"Monitor-{host}-{os.path.basename(fpath)}"
                )
                monitor_threads.append(t)
                t.start()

        # Step 4: Run client threads
        client_threads = [
            threading.Thread(
                target=build_and_run_client,
                kwargs=dict(
                    hostname="convsrc1",
                    command="sudo ./tcp_client_er -s 172.16.1.1 -p 2000 -n 250000 -c 500 -w 1 -a 172.16.1.10-22 -k -r 10000-65000"
                ),
                name="Client-tcp-convsrc1"
            ),
            threading.Thread(
                target=build_and_run_client,
                kwargs=dict(
                    hostname="convsrc1",
                    command="./udp_client_sub -s 172.16.1.1 -p 3000 -n 250000 -c 500 -a 172.16.1.10-22 -r 10000-65000"
                ),
                name="Client-udp-convsrc1"
            )#,
            # threading.Thread(
            #     target=build_and_run_client,
            #     kwargs=dict(
            #         hostname="convsrc2",
            #         command="./udp_client_sub -s 172.16.1.1 -p 3000 -n 250000 -c 250 -a 172.16.1.26-39 -r 10000-65000"
            #     ),
            #     name="Client-udp-convsrc2"
            # ),
            # threading.Thread(
            #     target=build_and_run_client,
            #     kwargs=dict(
            #         hostname="convsrc2",
            #         command="sudo ./tcp_client_er -s 172.16.1.1 -p 2000 -n 250000 -c 250 -w 1 -a 172.16.1.26-39 -k -r 10000-65000"
            #     ),
            #     name="Client-tcp-convsrc2"
            # )
        ]

        for ct in client_threads:
            ct.start()

        for ct in client_threads:
            ct.join()

        print(f"{GREEN}All client threads completed. Stopping monitoring...{RESET}")

        # Step 5: Stop log monitoring and join monitor threads
        stop_event.set()
        for mt in monitor_threads:
            mt.join()

        print(f"{GREEN}=== Completed Experimentation Iteration {i+1}/{repeats} ===\n{RESET}")

def main(experimentation_name):
    def pre_kill_conflicting_processes(client, host, programs):
        for prog in programs:
            check_cmd = f"pgrep -f {prog}"
            status, output = run_command_with_timeout(client, check_cmd, 5, hostname=host)
            if status and output.strip():
                pids = output.strip().splitlines()
                with print_lock:
                    print(f"{YELLOW}[{host}] Found '{prog}' with PIDs: {', '.join(pids)}. Killing...{RESET}")
                for pid in pids:
                    run_command_with_timeout(client, f"sudo kill -9 {pid}", 5, hostname=host)
                time.sleep(1)
            else:
                with print_lock:
                    print(f"{GREEN}[{host}] No '{prog}' running.{RESET}")

    def cleanup_logging_scripts():
        for host, _, program_file, working_dir in log_configs:
            client = connect_to_host(host)
            kill_cmd = f"cd {working_dir} && sudo ./{program_file} -k"
            run_command_with_timeout(client, kill_cmd, 5, hostname=host)
            check_cmd = f"ps aux | grep {program_file} | grep -v grep"
            status, output = run_command_with_timeout(client, check_cmd, 5, hostname=host)
            if output.strip():
                run_command_with_timeout(client, f"pkill -2 -f {program_file}", 5, hostname=host)
                time.sleep(2)
                status, output = run_command_with_timeout(client, check_cmd, 5, hostname=host)
                if output.strip():
                    run_command_with_timeout(client, f"pkill -9 -f {program_file}", 5, hostname=host)
            client.close()

    def start_logging_script(host, cmd, program_file, working_dir):
        client = connect_to_host(host)

        # Kill potential previous processes
        kill_list = ["conntrackAnalysis.py"] if program_file == "conntrackAnalysis.py" else ["start.sh", "cm_monitor.py", "n_monitor.py"]
        pre_kill_conflicting_processes(client, host, kill_list)

        # Start script
        full_cmd = f"cd {working_dir} && {cmd}"
        status, output = run_command_with_timeout(client, full_cmd, 10, hostname=host)
        if not status:
            with print_lock:
                print(f"{RED}[{host}] Failed to start '{cmd}':\n{output.strip()}{RESET}")
            client.close()
            return None

        time.sleep(2)
        # Verify process
        pid_cmd = f"pgrep -f {program_file}"
        status, output = run_command_with_timeout(client, pid_cmd, 5, hostname=host)
        pid = output.strip()
        if not status or not pid.isdigit():
            with print_lock:
                print(f"{RED}[{host}] Could not detect PID for '{program_file}':\n{output.strip()}{RESET}")
            client.close()
            return None

        verify_cmd = f"ps -p {pid} -o pid="
        status, output = run_command_with_timeout(client, verify_cmd, 5, hostname=host)
        if not status or output.strip() != pid:
            with print_lock:
                print(f"{RED}[{host}] Process PID {pid} not running.{RESET}")
            client.close()
            return None

        with print_lock:
            print(f"{GREEN}[{host}] {program_file} running with PID: {pid}{RESET}")
        client.close()
        return pid

    def pre_experimentation():
        matches = check()
        if matches < 4:
            print(f"{RED}Not enough matches to start. Required: 4, Found: {matches}{RESET}")
            return False

        print(f"{GREEN}Starting experimentation: {experiment_name}{RESET}")
        for host, cmd, program_file, working_dir in log_configs:
            print(f"{YELLOW}[{host}] Launching: {cmd}{RESET}")
            if not start_logging_script(host, cmd, program_file, working_dir):
                cleanup_logging_scripts()
                return False
        print(f"{GREEN}Pre-experimentation completed for '{experiment_name}'.{RESET}")
        return True

    # Configs
    experiment_name = experimentation_name
    CAlog = "/tmp/CA.log"
    log_configs = [
        ("connt1", f"sudo ./start.sh -i 1 -l /var/log/exp/{experiment_name} -p conntrackd --iface enp3s0 -d", "start.sh", "/opt/MasterThesis/CMNpsutil/"),
        ("convsrc2", f"sudo ./conntrackAnalysis.py -a connt1 -b connt2 -l /var/log/conntrack.log -o /var/log/exp/{experiment_name}_ca.csv -d -D -L {CAlog}", "conntrackAnalysis.py", "/opt/MasterThesis/connectiontrackingAnalysis/")
    ]

    # Run phases
    if not pre_experimentation():
        return
    run_experimentation(repeats=5, experimentation_name=experiment_name)
    print(f"{YELLOW}Post-experimentation phase starting...{RESET}")
    cleanup_logging_scripts()
    print(f"{GREEN}Post-experimentation cleanup complete for '{experiment_name}'.{RESET}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python script.py <experimentation_name>")
        sys.exit(1)
    experimentation_name = sys.argv[1]
    main(experimentation_name)
