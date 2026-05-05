document.addEventListener('DOMContentLoaded', () => {
    const scanInput = document.getElementById('scanInput');
    const statusBadge = document.getElementById('statusBadge');
    const detailsForm = document.getElementById('detailsForm');
    const recentTableBody = document.querySelector('#recentTable tbody');
    const fileListBody = document.getElementById('fileListBody');
    const finalizeBtn = document.getElementById('finalizeBtn');
    const clearBtn = document.getElementById('clearBtn'); // Reset Batch Button

    loadRecent();
    loadCompletedFiles();

    // --- RESET BATCH LOGIC ---
    clearBtn.addEventListener('click', async () => {
        if (!confirm("⚠️ WARNING: This will DELETE all items in the current batch.\n\nAre you sure you want to start over?")) {
            return;
        }

        try {
            // Relative path 'reset_batch' handles /CRC prefix automatically
            const res = await fetch('reset_batch', { method: 'POST' });
            if (res.ok) {
                // 1. Clear UI
                recentTableBody.innerHTML = '';

                // 2. Clear inputs
                document.getElementById('equipType').value = '';
                document.getElementById('itemDesc').value = '';
                document.getElementById('serialNum').value = '';
                document.getElementById('templeTag').value = '';
                scanInput.value = '';

                // 3. Reset Badge
                statusBadge.className = 'badge bg-secondary';
                statusBadge.innerText = 'Batch Reset';

                scanInput.focus();
                loadRecent(); // Should be empty now
            } else {
                alert("Error resetting batch.");
            }
        } catch (err) {
            console.error(err);
            alert("Network Error");
        }
    });

    // --- SCAN LOGIC ---
    scanInput.addEventListener('keydown', async (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            const term = scanInput.value.trim();
            if (!term) return;

            statusBadge.className = 'badge bg-warning text-dark';
            statusBadge.innerText = 'Searching Snipe...';

            try {
                // Relative path 'lookup'
                const res = await fetch('lookup', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ serial: term })
                });

                if (!res.ok) throw new Error("Server Error: " + res.status);

                const data = await res.json();

                // 1. Fill Fields
                document.getElementById('equipType').value = data['Equipment Type'] || '';
                document.getElementById('itemDesc').value = data['Item Description'] || '';
                document.getElementById('serialNum').value = data['Serial Number'] || '';
                document.getElementById('templeTag').value = data['Temple Tag'] || '';

                // 2. Status Badge Update
                if (data.found_in_snipe) {
                    statusBadge.className = 'badge bg-success';
                    statusBadge.innerText = 'Found in Snipe-IT';
                } else {
                    statusBadge.className = 'badge bg-danger';
                    statusBadge.innerText = 'Not Found - Verify Info';
                }

                // 3. Focus strategy
                if (!document.getElementById('serialNum').value) {
                    document.getElementById('serialNum').focus();
                } else {
                    document.getElementById('saveBtn').focus();
                }

            } catch (err) {
                console.error(err);
                statusBadge.className = 'badge bg-danger';
                statusBadge.innerText = 'Error';
            }
        }
    });

    // --- SAVE LOGIC ---
    detailsForm.addEventListener('submit', async (e) => {
        e.preventDefault();

        const payload = {
            "Equipment Type": document.getElementById('equipType').value,
            "Item Description": document.getElementById('itemDesc').value,
            "Serial Number": document.getElementById('serialNum').value,
            "Temple Tag": document.getElementById('templeTag').value
        };

        if (!payload['Serial Number']) {
            alert("Serial Number is required!");
            return;
        }

        try {
            // Relative path 'add'
            const res = await fetch('add', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const result = await res.json();

            if (result.ok) {
                // Clear and reset focus
                scanInput.value = '';
                document.getElementById('equipType').value = '';
                document.getElementById('itemDesc').value = '';
                document.getElementById('serialNum').value = '';
                document.getElementById('templeTag').value = '';

                statusBadge.className = 'badge bg-secondary';
                statusBadge.innerText = 'Waiting...';
                scanInput.focus();
                loadRecent();
            } else {
                alert("Error: " + result.error);
            }
        } catch (err) { console.error(err); }
    });

    // --- FINALIZE LOGIC ---
    finalizeBtn.addEventListener('click', async () => {
        if (!confirm("Finalize this batch? This will create the Excel file for CRC.")) return;
        try {
            // Relative path 'finalize'
            const res = await fetch('finalize', { method: 'POST' });
            const data = await res.json();
            if (data.ok) {
                loadRecent();
                loadCompletedFiles();
                alert("Batch saved: " + data.filename);
            } else { alert(data.error); }
        } catch (err) { alert("Network Error"); }
    });

    // --- ROW ACTIONS (EDIT & DELETE) ---
    async function deleteRow(serial) {
        if (!confirm(`Are you sure you want to remove serial ${serial} from this batch?`)) return;

        try {
            const response = await fetch("delete_row", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ serial: serial })
            });

            if (response.ok) {
                loadRecent();
            } else {
                alert("Failed to delete row.");
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
            return; // Nothing changed
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
                loadRecent();
            } else {
                alert("Failed to update row.");
            }
        } catch (err) {
            console.error(err);
        }
    }

    // --- HELPERS ---
    async function loadRecent() {
        try {
            const res = await fetch('recent');
            const data = await res.json();
            recentTableBody.innerHTML = '';

            data.items.forEach(row => {
                const tr = document.createElement('tr');

                const type = row[0] || "";
                const desc = row[1] || "";
                const serial = row[2] || "";
                const tag = row[3] || "";

                // Populate text columns
                tr.innerHTML = `
                    <td>${type}</td> 
                    <td>${desc}</td> 
                    <td class="fw-bold">${serial}</td> 
                    <td>${tag}</td>
                `;

                // Build Actions column dynamically to avoid string escaping issues
                const actionsTd = document.createElement('td');
                actionsTd.className = "text-end";

                const editBtn = document.createElement('button');
                editBtn.className = "btn btn-sm btn-outline-secondary me-1";
                editBtn.innerText = "Edit";
                editBtn.onclick = () => promptEditRow(type, desc, serial, tag);

                const deleteBtn = document.createElement('button');
                deleteBtn.className = "btn btn-sm btn-outline-danger";
                deleteBtn.innerText = "Delete";
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
            fileListBody.innerHTML = '';
            data.files.forEach(f => {
                const tr = document.createElement('tr');
                tr.innerHTML = `
                    <td>${f}</td>
                    <td><a href="download/${f}" class="btn btn-sm btn-outline-temple">Download</a></td>
                `;
                fileListBody.appendChild(tr);
            });
        } catch (err) {
            console.error("Error loading files:", err);
        }
    }
});