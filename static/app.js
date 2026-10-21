document.addEventListener('DOMContentLoaded', () => {
    const scanInput = document.getElementById('scanInput');
    const manualLookupBtn = document.getElementById('manualLookupBtn');
    const statusBadge = document.getElementById('statusBadge');
    const detailsForm = document.getElementById('detailsForm');
    const recentTableBody = document.querySelector('#recentTable tbody');
    const fileListBody = document.getElementById('fileListBody');
    const fileListBodyMobile = document.getElementById('fileListBodyMobile');
    const finalizeBtn = document.getElementById('finalizeBtn');
    const clearBtn = document.getElementById('clearBtn');
    const cameraScanBtn = document.getElementById('cameraScanBtn');
    const cameraRegion = document.getElementById('camera-region');
    const batchCount = document.getElementById('batchCount');
    const scanToast = document.getElementById('scanToast');

    let html5QrCode = null;
    let isScanning = false;

    loadRecent();
    loadCompletedFiles();

    // ========================
    // MOBILE TAB NAVIGATION
    // ========================
    const mobileTabs = document.getElementById('mobileTabs');
    if (mobileTabs) {
        mobileTabs.querySelectorAll('button').forEach(btn => {
            btn.addEventListener('click', () => {
                // Deactivate all tabs
                mobileTabs.querySelectorAll('button').forEach(b => b.classList.remove('active'));
                document.querySelectorAll('.tab-content-section').forEach(s => s.classList.remove('active'));

                // Activate clicked tab
                btn.classList.add('active');
                const target = document.getElementById(btn.dataset.tab);
                if (target) target.classList.add('active');
            });
        });
    }

    // ========================
    // TOAST NOTIFICATION
    // ========================
    function showToast(message, isError = false) {
        scanToast.textContent = message;
        scanToast.className = 'scan-toast show' + (isError ? ' error' : '');
        setTimeout(() => { scanToast.className = 'scan-toast'; }, 2000);
    }

    // ========================
    // CAMERA SCANNER (html5-qrcode)
    // Supports: QR, Code128, Code39, EAN-13, EAN-8, UPC-A, UPC-E, ITF, etc.
    // ========================
    cameraScanBtn.addEventListener('click', () => {
        if (isScanning) {
            stopCamera();
        } else {
            startCamera();
        }
    });

    // Auto-restart camera with new format filters if user changes mode during scan
    document.querySelectorAll('input[name="scanMode"]').forEach(radio => {
        radio.addEventListener('change', () => {
            if (isScanning) {
                stopCamera();
                setTimeout(startCamera, 150);
            }
        });
    });

    function startCamera() {
        cameraRegion.style.display = 'block';
        cameraScanBtn.textContent = '⏹ Stop Camera';
        cameraScanBtn.classList.add('scanning');
        isScanning = true;

        const mode = document.querySelector('input[name="scanMode"]:checked')?.value || 'barcode';

        let formatsToSupport;
        let qrboxFn;

        if (mode === 'barcode') {
            // Strictly 1D equipment barcodes (Lenovo, Dell, HP) - IGNORES all QR codes
            formatsToSupport = [
                Html5QrcodeSupportedFormats.CODE_128,
                Html5QrcodeSupportedFormats.CODE_39,
                Html5QrcodeSupportedFormats.CODE_93,
                Html5QrcodeSupportedFormats.ITF,
                Html5QrcodeSupportedFormats.CODABAR,
                Html5QrcodeSupportedFormats.EAN_13,
                Html5QrcodeSupportedFormats.UPC_A
            ];
            qrboxFn = function(viewfinderWidth, viewfinderHeight) {
                // Wide horizontal slot matching equipment barcode strips
                return { width: Math.floor(viewfinderWidth * 0.95), height: 140 };
            };
        } else if (mode === 'qr') {
            // Strictly 2D codes (Temple University Asset Tags)
            formatsToSupport = [
                Html5QrcodeSupportedFormats.QR_CODE,
                Html5QrcodeSupportedFormats.DATA_MATRIX
            ];
            qrboxFn = function(viewfinderWidth, viewfinderHeight) {
                const minDim = Math.min(viewfinderWidth, viewfinderHeight);
                const size = Math.floor(minDim * 0.75);
                return { width: size, height: size };
            };
        } else {
            // Both modes enabled
            formatsToSupport = [
                Html5QrcodeSupportedFormats.QR_CODE,
                Html5QrcodeSupportedFormats.CODE_128,
                Html5QrcodeSupportedFormats.CODE_39,
                Html5QrcodeSupportedFormats.CODE_93,
                Html5QrcodeSupportedFormats.ITF,
                Html5QrcodeSupportedFormats.CODABAR,
                Html5QrcodeSupportedFormats.EAN_13,
                Html5QrcodeSupportedFormats.UPC_A,
                Html5QrcodeSupportedFormats.DATA_MATRIX,
                Html5QrcodeSupportedFormats.PDF_417
            ];
            qrboxFn = function(viewfinderWidth, viewfinderHeight) {
                return { width: Math.floor(viewfinderWidth * 0.95), height: 240 };
            };
        }

        html5QrCode = new Html5Qrcode("camera-region", {
            formatsToSupport: formatsToSupport,
            verbose: false
        });

        const config = {
            fps: 10,
            qrbox: qrboxFn,
            disableFlip: false
        };

        // html5-qrcode strictly requires cameraIdOrConfig to have EXACTLY 1 key: facingMode
        html5QrCode.start(
            { facingMode: "environment" },
            config,
            onScanSuccess,
            onScanFailure
        ).then(() => {
            // Add red alignment guide laser line inside viewfinder for 1D barcodes
            if (mode === 'barcode' || mode === 'both') {
                const existingLaser = cameraRegion.querySelector('.barcode-laser');
                if (!existingLaser) {
                    const laser = document.createElement('div');
                    laser.className = 'barcode-laser';
                    cameraRegion.appendChild(laser);
                }
            }
        }).catch(err => {
            console.error("Camera start error:", err);
            showToast("Camera error: " + (err.name || err.message || "access denied"), true);
            stopCamera();
        });
    }

    function stopCamera() {
        if (html5QrCode) {
            html5QrCode.stop().then(() => {
                html5QrCode.clear();
                html5QrCode = null;
            }).catch(err => console.error("Camera stop error:", err));
        }
        const laser = cameraRegion.querySelector('.barcode-laser');
        if (laser) laser.remove();

        cameraRegion.style.display = 'none';
        cameraScanBtn.textContent = '📷 Tap to Scan';
        cameraScanBtn.classList.remove('scanning');
        isScanning = false;
    }

    let lastScanTime = 0;
    const SCAN_COOLDOWN_MS = 2000; // Prevent rapid duplicate scans

    function sanitizeScannedText(text) {
        let clean = (text || '').trim();
        // Extract serial if user accidentally scanned a Lenovo support QR URL
        if (clean.toLowerCase().includes('lenovo.com') || clean.toLowerCase().includes('pcsupport')) {
            const parts = clean.split('/').filter(p => p.length > 0);
            if (parts.length > 0) {
                const candidate = parts[parts.length - 1].split('?')[0].split('#')[0];
                if (candidate.length >= 6 && candidate.length <= 14) {
                    return candidate.toUpperCase();
                }
            }
        }
        return clean;
    }

    function onScanSuccess(decodedText, decodedResult) {
        const now = Date.now();
        if (now - lastScanTime < SCAN_COOLDOWN_MS) return;
        lastScanTime = now;

        const targetCode = sanitizeScannedText(decodedText);

        // Haptic feedback on supported devices
        if (navigator.vibrate) navigator.vibrate(100);

        // Flash the scan region
        cameraRegion.classList.add('scan-flash');
        setTimeout(() => cameraRegion.classList.remove('scan-flash'), 400);

        showToast('Scanned: ' + targetCode);

        // Stop camera after successful scan
        stopCamera();

        // Trigger lookup
        performLookup(targetCode);
    }

    function onScanFailure(error) {
        // Silence — this fires constantly when no code is in view
    }

    // ========================
    // MANUAL INPUT / KEYBOARD SCANNER
    // ========================
    scanInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            triggerManualLookup();
        }
    });

    manualLookupBtn.addEventListener('click', () => {
        triggerManualLookup();
    });

    function triggerManualLookup() {
        const term = scanInput.value.trim();
        if (!term) return;
        performLookup(term);
    }

    // ========================
    // LOOKUP (shared by camera and manual)
    // ========================
    async function performLookup(term) {
        statusBadge.className = 'badge bg-warning text-dark';
        statusBadge.innerText = 'Searching Snipe...';

        try {
            const res = await fetch('lookup', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ serial: term })
            });

            if (!res.ok) throw new Error("Server Error: " + res.status);
            const data = await res.json();

            // Fill fields
            document.getElementById('equipType').value = data['Equipment Type'] || '';
            document.getElementById('itemDesc').value = data['Item Description'] || '';
            document.getElementById('serialNum').value = data['Serial Number'] || '';
            document.getElementById('templeTag').value = data['Temple Tag'] || '';
            scanInput.value = term;

            // Status
            if (data.found_in_snipe) {
                statusBadge.className = 'badge bg-success';
                statusBadge.innerText = '✓ Found in Snipe-IT';
            } else {
                statusBadge.className = 'badge bg-danger';
                statusBadge.innerText = '✗ Not Found - Verify Info';
            }

            // Focus strategy — on mobile, don't auto-focus inputs (avoids keyboard popup)
            if (window.innerWidth >= 768) {
                if (!document.getElementById('serialNum').value) {
                    document.getElementById('serialNum').focus();
                } else {
                    document.getElementById('saveBtn').focus();
                }
            }

        } catch (err) {
            console.error(err);
            statusBadge.className = 'badge bg-danger';
            statusBadge.innerText = 'Error';
            showToast('Lookup failed', true);
        }
    }

    // ========================
    // SAVE / ADD TO BATCH
    // ========================
    detailsForm.addEventListener('submit', async (e) => {
        e.preventDefault();

        const payload = {
            "Equipment Type": document.getElementById('equipType').value,
            "Item Description": document.getElementById('itemDesc').value,
            "Serial Number": document.getElementById('serialNum').value,
            "Temple Tag": document.getElementById('templeTag').value
        };

        if (!payload['Serial Number']) {
            showToast('Serial Number is required!', true);
            return;
        }

        try {
            const res = await fetch('add', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const result = await res.json();

            if (result.ok) {
                // Clear fields
                scanInput.value = '';
                document.getElementById('equipType').value = '';
                document.getElementById('itemDesc').value = '';
                document.getElementById('serialNum').value = '';
                document.getElementById('templeTag').value = '';

                statusBadge.className = 'badge bg-success';
                statusBadge.innerText = '✓ Added!';
                showToast('Added to batch');

                // Haptic
                if (navigator.vibrate) navigator.vibrate(50);

                loadRecent();

                // Reset status after a moment
                setTimeout(() => {
                    statusBadge.className = 'badge bg-secondary';
                    statusBadge.innerText = 'Ready to Scan';
                }, 1500);

            } else {
                showToast('Error: ' + result.error, true);
                statusBadge.className = 'badge bg-danger';
                statusBadge.innerText = result.error;
            }
        } catch (err) {
            console.error(err);
            showToast('Network Error', true);
        }
    });

    // ========================
    // RESET BATCH
    // ========================
    clearBtn.addEventListener('click', async () => {
        if (!confirm("⚠️ WARNING: This will DELETE all items in the current batch.\n\nAre you sure you want to start over?")) {
            return;
        }

        try {
            const res = await fetch('reset_batch', { method: 'POST' });
            if (res.ok) {
                recentTableBody.innerHTML = '';
                document.getElementById('equipType').value = '';
                document.getElementById('itemDesc').value = '';
                document.getElementById('serialNum').value = '';
                document.getElementById('templeTag').value = '';
                scanInput.value = '';

                statusBadge.className = 'badge bg-secondary';
                statusBadge.innerText = 'Batch Reset';
                showToast('Batch cleared');
                loadRecent();
            } else {
                showToast('Error resetting batch', true);
            }
        } catch (err) {
            console.error(err);
            showToast('Network Error', true);
        }
    });

    // ========================
    // FINALIZE BATCH
    // ========================
    finalizeBtn.addEventListener('click', async () => {
        if (!confirm("Finalize this batch? This will create the Excel file for CRC.")) return;
        try {
            const res = await fetch('finalize', { method: 'POST' });
            const data = await res.json();
            if (data.ok) {
                loadRecent();
                loadCompletedFiles();
                showToast('Saved: ' + data.filename);
            } else {
                showToast(data.error, true);
            }
        } catch (err) {
            showToast('Network Error', true);
        }
    });

    // ========================
    // ROW ACTIONS (EDIT & DELETE)
    // ========================
    async function deleteRow(serial) {
        if (!confirm(`Remove serial "${serial}" from this batch?`)) return;

        try {
            const response = await fetch("delete_row", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ serial: serial })
            });

            if (response.ok) {
                showToast('Deleted');
                loadRecent();
            } else {
                showToast('Failed to delete', true);
            }
        } catch (err) {
            console.error(err);
        }
    }

    async function promptEditRow(oldType, oldDesc, oldSerial, oldTag) {
        const newType = prompt("Equipment Type:", oldType) ?? oldType;
        const newDesc = prompt("Item Description:", oldDesc) ?? oldDesc;
        const newSerial = prompt("Serial Number:", oldSerial) ?? oldSerial;
        const newTag = prompt("Temple Tag:", oldTag) ?? oldTag;

        if (newType === oldType && newDesc === oldDesc && newSerial === oldSerial && newTag === oldTag) {
            return;
        }

        try {
            const response = await fetch("edit_row", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    old_serial: oldSerial,
                    new_data: {
                        "Equipment Type": newType,
                        "Item Description": newDesc,
                        "Serial Number": newSerial,
                        "Temple Tag": newTag
                    }
                })
            });

            if (response.ok) {
                showToast('Updated');
                loadRecent();
            } else {
                showToast('Failed to update', true);
            }
        } catch (err) {
            console.error(err);
        }
    }

    // ========================
    // HELPERS
    // ========================
    async function loadRecent() {
        try {
            const res = await fetch('recent');
            const data = await res.json();
            recentTableBody.innerHTML = '';

            // Update batch count
            if (batchCount) {
                const count = data.items.length;
                batchCount.textContent = count > 0 ? `${count} item${count !== 1 ? 's' : ''} scanned` : 'No items yet';
            }

            data.items.forEach(row => {
                const tr = document.createElement('tr');

                const type = row[0] || "";
                const desc = row[1] || "";
                const serial = row[2] || "";
                const tag = row[3] || "";

                tr.innerHTML = `
                    <td>${escapeHtml(type)}</td>
                    <td>${escapeHtml(desc)}</td>
                    <td class="fw-bold">${escapeHtml(serial)}</td>
                    <td>${escapeHtml(tag)}</td>
                `;

                // Actions column
                const actionsTd = document.createElement('td');
                actionsTd.className = "text-end text-nowrap";

                const editBtn = document.createElement('button');
                editBtn.className = "btn btn-sm btn-outline-secondary me-1";
                editBtn.innerText = "✏️";
                editBtn.title = "Edit";
                editBtn.onclick = () => promptEditRow(type, desc, serial, tag);

                const deleteBtn = document.createElement('button');
                deleteBtn.className = "btn btn-sm btn-outline-danger";
                deleteBtn.innerText = "🗑";
                deleteBtn.title = "Delete";
                deleteBtn.onclick = () => deleteRow(serial);

                actionsTd.appendChild(editBtn);
                actionsTd.appendChild(deleteBtn);
                tr.appendChild(actionsTd);

                recentTableBody.appendChild(tr);
            });
        } catch (err) {
            console.error("Error loading recent rows:", err);
        }
    }

    async function loadCompletedFiles() {
        try {
            const res = await fetch('completed_files');
            const data = await res.json();

            // Populate both desktop and mobile file lists
            [fileListBody, fileListBodyMobile].forEach(tbody => {
                if (!tbody) return;
                tbody.innerHTML = '';
                data.files.forEach(f => {
                    const tr = document.createElement('tr');
                    tr.innerHTML = `
                        <td class="small">${escapeHtml(f)}</td>
                        <td class="text-end">
                            <a href="download/${encodeURIComponent(f)}" class="btn btn-sm btn-outline-temple">📥 Download</a>
                        </td>
                    `;
                    tbody.appendChild(tr);
                });
            });
        } catch (err) {
            console.error("Error loading files:", err);
        }
    }

    // Prevent XSS in table cells
    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }
});
