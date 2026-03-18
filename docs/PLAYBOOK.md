# BC10 Network Deployment Playbook

A step-by-step guide for deploying small, secure UniFi networks at new locations.

---

## Accessing Device Admin Panels

You'll need a spare patch cable and a USB/USB-C to RJ-45 (Ethernet) adapter for your laptop.
Connect the patch cable between a LAN port on the device and your laptop's adapter.

Open a browser and navigate to the device's IP address — always try **HTTPS first**
(e.g., `https://192.168.1.1`). Factory devices use self-signed SSL certificates, so your
browser will show a security warning. Click **"Advanced" → "Proceed anyway"** (or equivalent)
to continue. Do not use HTTP unless HTTPS is completely unavailable.

> Note: Incognito mode does NOT resolve SSL certificate warnings — you must accept the
> browser's security warning manually regardless of which browser mode you use.

---

## Step 1 — Stage with the Deployment Tool

Run this before going on-site. All devices should be on the bench and at factory defaults.

    a. Factory reset ALL devices before starting.
    b. Connect all devices using the same ports that will be used on-site.
    c. Run the deployment tool and verify all steps completed successfully.
    d. Upload the generated inventory and documentation files to the business documentation folder.
    e. Log into the gateway and verify VLAN settings and adopted devices look correct.

---

## Step 2 — Install Devices On-Site

    a. Mount network devices in their planned locations per pre-defined standards.
    b. Run Cat5e/Cat6 cables between all devices.
       DO NOT connect the modem to the gateway yet.
    c. Power on the gateway and switches. Log into the gateway and confirm all devices
       are showing as adopted and connected.

---

## Step 3 — Configure the Modem

Modem interfaces vary by ISP and model. Use this section as a guide and refer to the
modem's documentation or an AI assistant when needed.

    a. Log into the modem (typically at 192.168.100.1, or check the label on the modem).
    b. Disable any Wi-Fi networks broadcast by the modem — the UniFi APs will handle Wi-Fi.
    c. Connect the modem's LAN port to the gateway's WAN port.
    d. Set the modem to Bridge Mode to pass the public WAN IP directly to the gateway.

       IMPORTANT — If the ISP provides phone service over the same connection:
       Bridge mode will likely disable the phone line. Use IP Passthrough (or equivalent)
       instead. This passes the WAN IP to the gateway while keeping phone service active.

       Some ISPs (e.g. AT&T) require the modem's DHCP server to remain active to assign
       IP addresses to the phone equipment. In these cases, do NOT disable DHCP on the
       modem — the phones depend on it to stay online, even in IP Passthrough mode.

    e. You're done when the gateway's admin panel shows a public WAN IP address
       (e.g., 103.224.192.22) and the network's Wi-Fi SSIDs are broadcasting.

---

## Step 4 — Link the Gateway to a UniFi Cloud Account

Linking the gateway to a UI account enables remote management and monitoring.

    a. Log into the gateway's local admin panel at https://192.168.1.1
    b. Navigate to Settings → System → Remote Access.
    c. Sign in with your Ubiquiti (UI) account credentials.
    d. Once linked, the site will appear in UniFi Site Manager at unifi.ui.com
       where it can be remotely managed, monitored, and updated.
