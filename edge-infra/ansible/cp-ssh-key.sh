#! /bin/bash
export SSHPASS='bbk8s'
awk '{print $1}' servers.txt | while read host; do
  echo "Copying key to $host..."
  sshpass -e ssh-copy-id -i ~/.ssh/5G-lab-2026.pub ubuntu@$host
done
unset SSHPASS
