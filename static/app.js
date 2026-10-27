// Global interceptor for expired SSO session
const _originalFetch = window.fetch;
window.fetch = async function(...args) {
    const res = await _originalFetch.apply(this, args);
    if (res.status === 401) {
        window.location.href = '/login';
    }
    return res;
};

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
    const cameraWrapper = document.getElementById('cameraWrapper');
    const cameraRegion = document.getElementById('camera-region');
    const batchCount = document.getElementById('batchCount');
    const scanToast = document.getElementById('scanToast');

    let html5QrCode = null;
    let isScanning = false;
    let nativeScanInterval = null;
    let currentZoom = 1.0;
    let isTorchOn = false;

    const cameraControls = document.getElementById('cameraControls');
    const zoomBtn = document.getElementById('zoomBtn');
    const torchBtn = document.getElementById('torchBtn');
    const ocrCaptureBtn = document.getElementById('ocrCaptureBtn');
    const photoScanBtn = document.getElementById('photoScanBtn');
    const cameraPhotoInput = document.getElementById('cameraPhotoInput');
    const ocrAimGuide = document.getElementById('ocrAimGuide');
    const ocrCandidatesContainer = document.getElementById('ocrCandidatesContainer');
    const ocrCandidatesList = document.getElementById('ocrCandidatesList');
    const closeCandidatesBtn = document.getElementById('closeCandidatesBtn');
    const closeCameraBtn = document.getElementById('closeCameraBtn');

    if (closeCameraBtn) {
        closeCameraBtn.addEventListener('click', stopCamera);
    }

    if (closeCandidatesBtn && ocrCandidatesContainer) {
        closeCandidatesBtn.addEventListener('click', () => {
            ocrCandidatesContainer.style.display = 'none';
        });
    }

    if (ocrCaptureBtn) {
        ocrCaptureBtn.addEventListener('click', captureAndProcessOcr);
    }

    if (photoScanBtn && cameraPhotoInput) {
        photoScanBtn.addEventListener('click', () => {
            cameraPhotoInput.click();
        });

        cameraPhotoInput.addEventListener('change', async () => {
            const file = cameraPhotoInput.files && cameraPhotoInput.files[0];
            if (!file) return;

            showToast("📸 Processing photo with OCR...");
            try {
                const formData = new FormData();
                formData.append('file', file);

                const res = await fetch('ocr', {
                    method: 'POST',
                    body: formData
                });

                if (!res.ok) {
                    throw new Error(`Server returned status ${res.status}`);
                }

                const data = await res.json();
                processOcrResult(data);
            } catch (err) {
                console.error("Photo OCR error:", err);
                showToast("Photo OCR failed: " + (err.message || "Error"), true);
            } finally {
                cameraPhotoInput.value = '';
            }
        });
    }

    async function captureAndProcessOcr() {
        const video = cameraRegion.querySelector('video');
        if (!video || !video.videoWidth) {
            showToast("Camera stream not ready yet.", true);
            return;
        }

        if (ocrCaptureBtn) {
            ocrCaptureBtn.disabled = true;
            ocrCaptureBtn.textContent = '⏳ Reading...';
        }
        showToast("🔍 Reading text with OCR...");

        try {
            const canvas = document.createElement('canvas');
            canvas.width = video.videoWidth;
            canvas.height = video.videoHeight;
            const ctx = canvas.getContext('2d');
            ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

            const crop = {
                normalized: true,
                x: 0.05,
                y: 0.15,
                width: 0.90,
                height: 0.70
            };

            const res = await fetch('ocr', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    image: canvas.toDataURL('image/jpeg', 0.90),
                    crop: crop
                })
            });

            if (!res.ok) {
                throw new Error(`Server returned ${res.status}`);
            }

            const data = await res.json();
            processOcrResult(data);
        } catch (err) {
            console.error("OCR capture failed:", err);
            showToast("OCR read failed: " + (err.message || "Network error"), true);
        } finally {
            if (ocrCaptureBtn) {
                ocrCaptureBtn.disabled = false;
                ocrCaptureBtn.textContent = '📸 Read Text';
            }
        }
    }

    function processOcrResult(data) {
        if (!data || !data.success) {
            showToast(data?.error || "No text detected. Try closer or better lighting.", true);
            return;
        }

        if (data.primary) {
            if (navigator.vibrate) navigator.vibrate(100);
            cameraRegion.classList.add('scan-flash');
            setTimeout(() => cameraRegion.classList.remove('scan-flash'), 400);

            showToast("✓ Detected: " + data.primary);
            scanInput.value = data.primary;
            stopCamera();
            performLookup(data.primary);
        } else {
            showToast("No serial / tag found in image. Try again.", true);
        }

        renderCandidates(data.candidates || []);
    }

    function renderCandidates(candidates) {
        if (!ocrCandidatesContainer || !ocrCandidatesList) return;
        ocrCandidatesList.innerHTML = '';

        if (!candidates || candidates.length === 0) {
            ocrCandidatesContainer.style.display = 'none';
            return;
        }

        candidates.forEach(c => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'btn btn-sm btn-outline-secondary d-flex align-items-center gap-1';
            btn.style.fontSize = '0.82rem';
            btn.innerHTML = `<span class="badge bg-light text-dark border">${c.label}</span> <strong>${c.value}</strong>`;
            btn.addEventListener('click', () => {
                scanInput.value = c.value;
                performLookup(c.value);
                ocrCandidatesContainer.style.display = 'none';
            });
            ocrCandidatesList.appendChild(btn);
        });

        ocrCandidatesContainer.style.display = 'block';
    }

    if (zoomBtn) {
        zoomBtn.addEventListener('click', async () => {
            if (!html5QrCode || !isScanning) return;
            try {
                const caps = html5QrCode.getRunningTrackCapabilities() || {};
                const maxZ = (caps.zoom && caps.zoom.max) || 2.0;
                currentZoom = currentZoom <= 1.0 ? Math.min(2.5, maxZ) : 1.0;
                await html5QrCode.applyVideoConstraints({ advanced: [{ zoom: currentZoom }] });
                zoomBtn.textContent = currentZoom > 1.0 ? '🔍 1x' : '🔍 2x';
                zoomBtn.classList.toggle('active-state', currentZoom > 1.0);
            } catch (e) {
                console.warn('Zoom failed:', e);
            }
        });
    }

    if (torchBtn) {
        torchBtn.addEventListener('click', async () => {
            if (!html5QrCode || !isScanning) return;
            try {
                isTorchOn = !isTorchOn;
                await html5QrCode.applyVideoConstraints({ advanced: [{ torch: isTorchOn }] });
                torchBtn.textContent = isTorchOn ? '🔦 Off' : '🔦 Light';
                torchBtn.classList.toggle('active-state', isTorchOn);
            } catch (e) {
                console.warn('Torch failed:', e);
            }
        });
    }

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
                window.scrollTo({ top: 0, behavior: 'smooth' });
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
        if (cameraWrapper) cameraWrapper.style.display = 'block';
        cameraRegion.style.display = 'block';
        cameraScanBtn.textContent = '⏹ Stop Camera';
        cameraScanBtn.classList.add('scanning');
        isScanning = true;

        const mode = document.querySelector('input[name="scanMode"]:checked')?.value || 'ocr';

        let formatsToSupport;
        let qrboxFn;

        if (mode === 'ocr') {
            // Text / OCR Mode: Camera stream for live frame capture
            formatsToSupport = [Html5QrcodeSupportedFormats.QR_CODE];
            qrboxFn = function(viewfinderWidth, viewfinderHeight) {
                return { width: Math.floor(viewfinderWidth * 0.92), height: Math.floor(viewfinderHeight * 0.65) };
            };
        } else {
            // QR Mode: 2D asset tags (Temple QR, Data Matrix)
            formatsToSupport = [
                Html5QrcodeSupportedFormats.QR_CODE,
                Html5QrcodeSupportedFormats.DATA_MATRIX
            ];
            qrboxFn = function(viewfinderWidth, viewfinderHeight) {
                const minDim = Math.min(viewfinderWidth, viewfinderHeight);
                const size = Math.floor(minDim * 0.75);
                return { width: size, height: size };
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
        ).then(async () => {
            // Hardware capabilities (Zoom & Torch)
            let caps = {};
            try {
                caps = html5QrCode.getRunningTrackCapabilities() || {};
            } catch (e) {}

            let hasControls = false;
            if (caps.zoom && caps.zoom.max > 1.0) {
                zoomBtn.style.display = 'inline-flex';
                zoomBtn.textContent = '🔍 2x';
                zoomBtn.className = 'btn btn-dark-overlay';
                currentZoom = 1.0;
                hasControls = true;
            } else {
                zoomBtn.style.display = 'none';
            }

            if (caps.torch) {
                torchBtn.style.display = 'inline-flex';
                torchBtn.textContent = '🔦 Light';
                torchBtn.className = 'btn btn-dark-overlay';
                isTorchOn = false;
                hasControls = true;
            } else {
                torchBtn.style.display = 'none';
            }

            // In OCR mode: show OCR capture button and top aiming guide
            if (mode === 'ocr') {
                if (ocrCaptureBtn) ocrCaptureBtn.style.display = 'inline-flex';
                if (ocrAimGuide) ocrAimGuide.style.display = 'block';
                const existingReticle = cameraRegion.querySelector('.ocr-reticle');
                if (!existingReticle) {
                    const reticle = document.createElement('div');
                    reticle.className = 'ocr-reticle';
                    reticle.innerHTML = `
                        <span class="bracket bracket-tl"></span>
                        <span class="bracket bracket-tr"></span>
                        <span class="bracket bracket-bl"></span>
                        <span class="bracket bracket-br"></span>
                        <span class="ocr-reticle-label">Align Serial / Label</span>
                    `;
                    cameraRegion.appendChild(reticle);
                }
                hasControls = true;
            } else {
                if (ocrCaptureBtn) ocrCaptureBtn.style.display = 'none';
                if (ocrAimGuide) ocrAimGuide.style.display = 'none';
                const reticle = cameraRegion.querySelector('.ocr-reticle');
                if (reticle) reticle.remove();
            }

            if (hasControls && cameraControls) {
                cameraControls.style.display = 'flex';
            }

            // High-Resolution Native BarcodeDetector for QR mode
            if (mode === 'qr' && 'BarcodeDetector' in window) {
                try {
                    const supportedFormats = await BarcodeDetector.getSupportedFormats();
                    const desiredFormats = ['qr_code', 'data_matrix'];
                    const finalFormats = desiredFormats.filter(f => supportedFormats.includes(f));

                    if (finalFormats.length > 0) {
                        const detector = new BarcodeDetector({ formats: finalFormats });
                        const videoEl = cameraRegion.querySelector('video');
                        if (videoEl) {
                            if (nativeScanInterval) clearInterval(nativeScanInterval);
                            nativeScanInterval = setInterval(async () => {
                                if (!isScanning || videoEl.readyState < 2) return;
                                try {
                                    const results = await detector.detect(videoEl);
                                    if (results && results.length > 0) {
                                        const raw = results[0].rawValue;
                                        if (raw) {
                                            clearInterval(nativeScanInterval);
                                            onScanSuccess(raw);
                                        }
                                    }
                                } catch (detErr) {}
                            }, 100);
                        }
                    }
                } catch (bErr) {
                    console.warn('Native BarcodeDetector not available:', bErr);
                }
            }
        }).catch(err => {
            console.error("Camera start error:", err);
            showToast("Camera error: " + (err.name || err.message || "access denied"), true);
            stopCamera();
        });
    }

    function stopCamera() {
        if (nativeScanInterval) {
            clearInterval(nativeScanInterval);
            nativeScanInterval = null;
        }
        if (cameraControls) {
            cameraControls.style.display = 'none';
        }
        if (html5QrCode) {
            html5QrCode.stop().then(() => {
                html5QrCode.clear();
                html5QrCode = null;
            }).catch(err => console.error("Camera stop error:", err));
        }
        const reticle = cameraRegion.querySelector('.ocr-reticle');
        if (reticle) reticle.remove();
        if (ocrAimGuide) ocrAimGuide.style.display = 'none';

        if (cameraWrapper) cameraWrapper.style.display = 'none';
        cameraRegion.style.display = 'none';
        cameraScanBtn.textContent = '📹 Live Camera';
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

            // Update batch count & mobile bottom tab badge
            const count = (data.items || []).length;
            if (batchCount) {
                batchCount.textContent = count > 0 ? `${count} item${count !== 1 ? 's' : ''} scanned` : 'No items yet';
            }
            const batchTabBadge = document.getElementById('batchTabBadge');
            if (batchTabBadge) {
                batchTabBadge.textContent = count;
                batchTabBadge.style.display = count > 0 ? 'inline-block' : 'none';
            }

            if (count === 0) {
                const tr = document.createElement('tr');
                tr.innerHTML = `<td colspan="5" class="text-center text-muted py-4"><span style="font-size:1.8rem; display:block; margin-bottom:4px;">📦</span>No equipment added to this batch yet.</td>`;
                recentTableBody.appendChild(tr);
                return;
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
