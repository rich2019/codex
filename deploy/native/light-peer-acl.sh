#!/usr/bin/env bash
set -euo pipefail

iptables -C INPUT -i eth0 ! -s 10.0.7.126/32 -p tcp --dport 8000 -j DROP 2>/dev/null \
    || iptables -I INPUT 1 -i eth0 ! -s 10.0.7.126/32 -p tcp --dport 8000 -j DROP
iptables -C INPUT -i eth0 ! -s 10.0.7.126/32 -d 172.24.53.149/32 -p tcp --dport 9107 -j DROP 2>/dev/null \
    || iptables -I INPUT 1 -i eth0 ! -s 10.0.7.126/32 -d 172.24.53.149/32 -p tcp --dport 9107 -j DROP
iptables -C DOCKER-USER -i eth0 ! -s 10.0.7.126/32 -p tcp -m conntrack --ctstate NEW --ctorigdst 172.24.53.149 --ctorigdstport 8000 -j DROP 2>/dev/null \
    || iptables -I DOCKER-USER 1 -i eth0 ! -s 10.0.7.126/32 -p tcp -m conntrack --ctstate NEW --ctorigdst 172.24.53.149 --ctorigdstport 8000 -j DROP
iptables -C DOCKER-USER -i eth0 ! -s 10.0.7.126/32 -p tcp -m conntrack --ctstate NEW --ctorigdst 172.24.53.149 --ctorigdstport 9107 -j DROP 2>/dev/null \
    || iptables -I DOCKER-USER 1 -i eth0 ! -s 10.0.7.126/32 -p tcp -m conntrack --ctstate NEW --ctorigdst 172.24.53.149 --ctorigdstport 9107 -j DROP
