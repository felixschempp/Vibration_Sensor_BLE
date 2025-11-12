# Select the USB dongle (hci1) as the active BLE adapter and disable the internal (hci0)
```bash
sudo btmgmt -i hci1 power on
sudo btmgmt -i hci0 power off
```

# Configure for BLE use
```bash
sudo btmgmt -i hci1 le on
sudo btmgmt -i hci1 bredr off || true    # some adapters reject this; harmless
sudo btmgmt -i hci1 connectable on
sudo btmgmt -i hci1 bondable off         # no pairing needed for this app
```
