# RATAN Engine — Multi-Path Test Lab
# Based on mptcp-vagrant topology (3 independent paths between 2 VMs)
# Used for testing RATAN aggregation without physical hardware.
#
# Usage:
#   vagrant up        # Create VMs
#   ./scripts/test_lab_setup.sh  # Deploy RATAN and test

Vagrant.configure("2") do |config|
  config.vm.box = "ubuntu/jammy64"

  # ── Client VM (simulates RPi edge node) ──────────────────────────
  config.vm.define "client" do |c|
    c.vm.hostname = "ratan-client"
    c.vm.network "private_network", ip: "192.168.56.100"  # Path 1 (Starlink sim)
    c.vm.network "private_network", ip: "192.168.57.100"  # Path 2 (Cellular sim)
    c.vm.network "private_network", ip: "192.168.58.100"  # Path 3 (Backup sim)

    c.vm.provider "virtualbox" do |vb|
      vb.memory = 1024
      vb.cpus = 2
      vb.name = "ratan-client"
    end

    c.vm.provision "shell", inline: <<-SHELL
      apt-get update -qq
      apt-get install -y -qq python3-pip iproute2 iperf3 >/dev/null
    SHELL
  end

  # ── Server VM (simulates Hetzner VPS) ────────────────────────────
  config.vm.define "server" do |s|
    s.vm.hostname = "ratan-server"
    s.vm.network "private_network", ip: "192.168.56.101"  # Path 1
    s.vm.network "private_network", ip: "192.168.57.101"  # Path 2
    s.vm.network "private_network", ip: "192.168.58.101"  # Path 3

    s.vm.provider "virtualbox" do |vb|
      vb.memory = 1024
      vb.cpus = 2
      vb.name = "ratan-server"
    end

    s.vm.provision "shell", inline: <<-SHELL
      apt-get update -qq
      apt-get install -y -qq python3-pip iproute2 iperf3 >/dev/null
    SHELL
  end

  # Sync the RATAN project into both VMs
  config.vm.synced_folder ".", "/vagrant"
end
