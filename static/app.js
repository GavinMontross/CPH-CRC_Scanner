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
    // Photo Scanner Elements (1-Tap Direct Camera & Library Upload)
    const takePhotoBtn = document.getElementById('takePhotoBtn');
    const cameraDirectInput = document.getElementById('cameraDirectInput');
    const chooseLibraryBtn = document.getElementById('chooseLibraryBtn');
    const libraryPhotoInput = document.getElementById('libraryPhotoInput');
    const takePhotoTitle = document.getElementById('takePhotoTitle');
    const takePhotoSub = document.getElementById('takePhotoSub');
    const photoStatusBadge = document.getElementById('photoStatusBadge');

    // Form & Manual Lookup Elements
    const scanInput = document.getElementById('scanInput');
    const manualLookupBtn = document.getElementById('manualLookupBtn');
    const statusBadge = document.getElementById('statusBadge');
    const detailsForm = document.getElementById('detailsForm');
    const equipType = document.getElementById('equipType');
    const itemDesc = document.getElementById('itemDesc');
    const serialNum = document.getElementById('serialNum');
    const templeTag = document.getElementById('templeTag');
    const clearFormBtn = document.getElementById('clearFormBtn');
    const saveBtn = document.getElementById('saveBtn');

    // Batch & Files Elements
    const recentTableBody = document.querySelector('#recentTable tbody');
    const batchCountBadge = document.getElementById('batchCountBadge');
    const batchTabBadge = document.getElementById('batchTabBadge');
    const clearBtn = document.getElementById('clearBtn');
    const finalizeBtn = document.getElementById('finalizeBtn');
    const fileListBody = document.getElementById('fileListBody');
    const refreshFilesBtn = document.getElementById('refreshFilesBtn');

    // Candidates & Toast Elements
    const scanToast = document.getElementById('scanToast');
    const ocrCandidatesContainer = document.getElementById('ocrCandidatesContainer');
    const ocrCandidatesList = document.getElementById('ocrCandidatesList');
    const closeCandidatesBtn = document.getElementById('closeCandidatesBtn');

    // Edit Modal Elements
    const modalEl = document.getElementById('editItemModal');
    let editModalInstance = null;

    // ==========================================
    // 1-TAP PHOTO SCANNER (Direct Camera & Upload)
    // ==========================================
    if (takePhotoBtn && cameraDirectInput) {
        takePhotoBtn.addEventListener('click', () => {
            // Directly launches native camera on mobile with zero dialogs/prompts
            cameraDirectInput.click();
        });

        cameraDirectInput.addEventListener('change', () => {
            handlePhotoFile(cameraDirectInput);
        });
    }

    if (chooseLibraryBtn && libraryPhotoInput) {
        chooseLibraryBtn.addEventListener('click', () => {
            libraryPhotoInput.click();
        });

        libraryPhotoInput.addEventListener('change', () => {
            handlePhotoFile(libraryPhotoInput);
        });
    }

    async function handlePhotoFile(inputEl) {
        const file = inputEl.files && inputEl.files[0];
        if (!file) return;

        showToast("📸 Reading barcode from photo...");

        if (takePhotoBtn) {
            takePhotoBtn.disabled = true;
            if (takePhotoTitle) takePhotoTitle.textContent = "Scanning Barcode...";
            if (takePhotoSub) takePhotoSub.textContent = "Decoding barcode serial & matching in Snipe-IT";
        }
        if (photoStatusBadge) {
            photoStatusBadge.style.display = 'inline';
            photoStatusBadge.textContent = '⏳ Processing...';
        }

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
            console.error("Photo scan error:", err);
            showToast("Scan failed: " + (err.message || "Network error"), true);
        } finally {
            inputEl.value = '';
            if (takePhotoBtn) {
                takePhotoBtn.disabled = false;
                if (takePhotoTitle) takePhotoTitle.textContent = "Take Photo to Scan";
                if (takePhotoSub) takePhotoSub.textContent = "Tap to open camera • Reads barcode serials";
            }
            if (photoStatusBadge) {
                photoStatusBadge.style.display = 'none';
            }
        }
    }

    function processOcrResult(data) {
        if (!data || !data.success) {
            showToast(data?.error || "No barcode found. Please ensure barcode is clear and in focus.", true);
            return;
        }

        if (data.primary) {
            if (navigator.vibrate) navigator.vibrate([40, 60, 40]);
            showToast("✓ Barcode: " + data.primary);
            scanInput.value = data.primary;
            performLookup(data.primary);
        } else {
            showToast("No barcode detected in photo. Try again.", true);
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

            const badge = document.createElement('span');
            badge.className = c.verified ? 'badge bg-success text-white' : 'badge bg-light text-dark border';
            badge.textContent = c.label;

            const val = document.createElement('strong');
            val.textContent = c.value;

            btn.appendChild(badge);
            btn.appendChild(val);

            btn.addEventListener('click', () => {
                scanInput.value = c.value;
                performLookup(c.value);
                ocrCandidatesContainer.style.display = 'none';
            });
            ocrCandidatesList.appendChild(btn);
        });

        ocrCandidatesContainer.style.display = 'block';
    }

    if (closeCandidatesBtn && ocrCandidatesContainer) {
        closeCandidatesBtn.addEventListener('click', () => {
            ocrCandidatesContainer.style.display = 'none';
        });
    }

    // ==========================================
    // MANUAL LOOKUP / KEYBOARD SEARCH
    // ==========================================
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

    async function performLookup(term) {
        statusBadge.className = 'status-pill bg-warning text-dark';
        statusBadge.innerText = 'Searching Snipe...';

        try {
            const res = await fetch('lookup', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ serial: term })
            });

            if (!res.ok) throw new Error("Server Error: " + res.status);
            const data = await res.json();

            // Auto-fill form fields
            equipType.value = data['Equipment Type'] || '';
            itemDesc.value = data['Item Description'] || '';
            serialNum.value = data['Serial Number'] || '';
            templeTag.value = data['Temple Tag'] || '';
            scanInput.value = term;

            // Status Badge
            if (data.found_in_snipe) {
                statusBadge.className = 'status-pill bg-success text-white';
                statusBadge.innerText = '✓ Found in Snipe-IT';
            } else {
                statusBadge.className = 'status-pill bg-danger text-white';
                statusBadge.innerText = '✗ Not in Snipe-IT (Manual)';
            }

            // Focus strategy on desktop
            if (window.innerWidth >= 768) {
                if (!serialNum.value) {
                    serialNum.focus();
                } else {
                    saveBtn.focus();
                }
            }
        } catch (err) {
            console.error("Lookup error:", err);
            statusBadge.className = 'status-pill bg-danger text-white';
            statusBadge.innerText = 'Error';
            showToast('Lookup failed', true);
        }
    }

    // ==========================================
    // DETAILS FORM (ADD TO BATCH & CLEAR)
    // ==========================================
    detailsForm.addEventListener('submit', async (e) => {
        e.preventDefault();

        const payload = {
            "Equipment Type": equipType.value.trim(),
            "Item Description": itemDesc.value.trim(),
            "Serial Number": serialNum.value.trim(),
            "Temple Tag": templeTag.value.trim()
        };

        if (!payload['Serial Number']) {
            showToast('Serial Number is required!', true);
            serialNum.focus();
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
                equipType.value = '';
                itemDesc.value = '';
                serialNum.value = '';
                templeTag.value = '';

                statusBadge.className = 'status-pill bg-success text-white';
                statusBadge.innerText = '✓ Added to Batch';
                showToast('Added to batch');

                if (navigator.vibrate) navigator.vibrate(50);
                loadRecent();

                setTimeout(() => {
                    statusBadge.className = 'status-pill';
                    statusBadge.innerText = 'Ready to Scan';
                }, 1500);
            } else {
                showToast('Error: ' + result.error, true);
                statusBadge.className = 'status-pill bg-danger text-white';
                statusBadge.innerText = result.error;
            }
        } catch (err) {
            console.error("Add item error:", err);
            showToast('Network Error', true);
        }
    });

    if (clearFormBtn) {
        clearFormBtn.addEventListener('click', () => {
            scanInput.value = '';
            equipType.value = '';
            itemDesc.value = '';
            serialNum.value = '';
            templeTag.value = '';
            statusBadge.className = 'status-pill';
            statusBadge.innerText = 'Ready to Scan';
            if (ocrCandidatesContainer) ocrCandidatesContainer.style.display = 'none';
            showToast('Form cleared');
        });
    }

    // ==========================================
    // BATCH MANAGEMENT (RESET & FINALIZE)
    // ==========================================
    clearBtn.addEventListener('click', async () => {
        if (!confirm("⚠️ WARNING: This will DELETE all items in the current batch.\n\nAre you sure you want to start over?")) {
            return;
        }

        try {
            const res = await fetch('reset_batch', { method: 'POST' });
            if (res.ok) {
                recentTableBody.innerHTML = '';
                equipType.value = '';
                itemDesc.value = '';
                serialNum.value = '';
                templeTag.value = '';
                scanInput.value = '';

                statusBadge.className = 'status-pill';
                statusBadge.innerText = 'Batch Reset';
                showToast('Batch cleared');
                loadRecent();
            } else {
                showToast('Error resetting batch', true);
            }
        } catch (err) {
            console.error("Reset batch error:", err);
            showToast('Network Error', true);
        }
    });

    finalizeBtn.addEventListener('click', async () => {
        if (!confirm("Finalize this batch? This will create the Excel file for CRC.")) return;
        try {
            const res = await fetch('finalize', { method: 'POST' });
            const data = await res.json();
            if (data.ok) {
                loadRecent();
                loadCompletedFiles();
                showToast('Batch finalized: ' + data.filename);
            } else {
                showToast(data.error || 'Failed to finalize batch', true);
            }
        } catch (err) {
            showToast('Network Error', true);
        }
    });

    // ==========================================
    // ROW ACTIONS (EDIT & DELETE)
    // ==========================================
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
                showToast('Failed to delete item', true);
            }
        } catch (err) {
            console.error("Delete row error:", err);
            showToast('Network error deleting item', true);
        }
    }

    function promptEditRow(oldType, oldDesc, oldSerial, oldTag) {
        if (modalEl && window.bootstrap) {
            document.getElementById('editOldSerial').value = oldSerial;
            document.getElementById('editEquipType').value = oldType;
            document.getElementById('editItemDesc').value = oldDesc;
            document.getElementById('editSerialNum').value = oldSerial;
            document.getElementById('editTempleTag').value = oldTag;

            if (!editModalInstance) {
                editModalInstance = new bootstrap.Modal(modalEl);
            }
            editModalInstance.show();
        } else {
            equipType.value = oldType;
            itemDesc.value = oldDesc;
            serialNum.value = oldSerial;
            templeTag.value = oldTag;
            window.scrollTo({ top: 0, behavior: 'smooth' });
            showToast('Loaded into form for editing');
        }
    }

    const saveEditModalBtn = document.getElementById('saveEditModalBtn');
    if (saveEditModalBtn) {
        saveEditModalBtn.addEventListener('click', async () => {
            const oldSerial = document.getElementById('editOldSerial').value;
            const newType = document.getElementById('editEquipType').value.trim();
            const newDesc = document.getElementById('editItemDesc').value.trim();
            const newSerial = document.getElementById('editSerialNum').value.trim();
            const newTag = document.getElementById('editTempleTag').value.trim();

            if (!newSerial) {
                showToast("Serial Number is required", true);
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
                    if (editModalInstance) editModalInstance.hide();
                    loadRecent();
                } else {
                    showToast('Failed to update', true);
                }
            } catch (err) {
                console.error("Edit modal error:", err);
                showToast('Network error updating item', true);
            }
        });
    }

    // ==========================================
    // RECENT BATCH ITEMS & COMPLETED FILES
    // ==========================================
    async function loadRecent() {
        try {
            const res = await fetch('recent');
            const data = await res.json();
            recentTableBody.innerHTML = '';

            const count = (data.items || []).length;
            if (batchCountBadge) {
                batchCountBadge.textContent = count === 1 ? '1 item' : `${count} items`;
            }
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

            if (fileListBody) {
                fileListBody.innerHTML = '';

                if (!data.files || data.files.length === 0) {
                    fileListBody.innerHTML = `<tr><td colspan="2" class="text-center text-muted py-4"><span style="font-size:1.6rem; display:block; margin-bottom:4px;">📁</span>No completed batches yet. Finish a batch to generate an Excel file.</td></tr>`;
                    return;
                }

                data.files.forEach(f => {
                    const tr = document.createElement('tr');
                    tr.innerHTML = `
                        <td class="small fw-semibold text-truncate" style="max-width: 240px;" title="${escapeHtml(f)}">
                            <span class="me-1">📊</span> ${escapeHtml(f)}
                        </td>
                        <td class="text-end text-nowrap">
                            <a href="/download/${encodeURIComponent(f)}" class="btn btn-sm btn-outline-danger rounded-pill fw-semibold px-3">⬇️ Download</a>
                        </td>
                    `;
                    fileListBody.appendChild(tr);
                });
            }
        } catch (err) {
            console.error("Error loading files:", err);
        }
    }

    if (refreshFilesBtn) {
        refreshFilesBtn.addEventListener('click', loadCompletedFiles);
    }
    window.loadCompletedFiles = loadCompletedFiles;

    // ==========================================
    // MOBILE BOTTOM TAB NAVIGATION
    // ==========================================
    const mobileTabs = document.getElementById('mobileTabs');
    if (mobileTabs) {
        mobileTabs.querySelectorAll('button').forEach(btn => {
            btn.addEventListener('click', () => {
                mobileTabs.querySelectorAll('button').forEach(b => b.classList.remove('active'));
                document.querySelectorAll('.tab-content-section').forEach(s => s.classList.remove('active'));

                btn.classList.add('active');
                const target = document.getElementById(btn.dataset.tab);
                if (target) target.classList.add('active');
                window.scrollTo({ top: 0, behavior: 'smooth' });
            });
        });
    }

    // ==========================================
    // TOAST NOTIFICATION
    // ==========================================
    function showToast(message, isError = false) {
        scanToast.textContent = message;
        scanToast.className = 'scan-toast show' + (isError ? ' error' : '');
        setTimeout(() => { scanToast.className = 'scan-toast'; }, 2200);
    }

    // Prevent XSS in table cells
    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    // Initial Load
    loadRecent();
    loadCompletedFiles();
});
