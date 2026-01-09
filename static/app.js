document.addEventListener('DOMContentLoaded', () => {
    const scanInput = document.getElementById('scanInput');
    const statusBadge = document.getElementById('statusBadge');
    const detailsForm = document.getElementById('detailsForm');
    const recentTableBody = document.querySelector('#recentTable tbody');
    const fileListBody = document.getElementById('fileListBody');
    const finalizeBtn = document.getElementById('finalizeBtn');

    loadRecent();
    loadCompletedFiles();

    // --- SCAN LOGIC ---
    scanInput.addEventListener('keydown', async (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            const term = scanInput.value.trim();
            if (!term) return;

            statusBadge.className = 'badge bg-warning text-dark';
            statusBadge.innerText = 'Searching Snipe...';

            try {
                const res = await fetch('/lookup', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ serial: term })
                });
                const data = await res.json();

                // 1. Fill Fields with the data returned (whether found or heuristic)
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
                // If serial is missing (e.g. they scanned a loose tag not in system), jump to serial box
                if (!document.getElementById('serialNum').value) {
                    document.getElementById('serialNum').focus();
                } else {
                    // Otherwise ready to save
                    document.getElementById('saveBtn').focus();
                }

            } catch (err) {
                console.error(err);
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
            const res = await fetch('/add', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
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
        if (!confirm("Finalize this batch? This will create the CSV for CRC.")) return;
        try {
            const res = await fetch('/finalize', { method: 'POST' });
            const data = await res.json();
            if (data.ok) {
                loadRecent();
                loadCompletedFiles();
            } else { alert(data.error); }
        } catch (err) { alert("Network Error"); }
    });

    // --- HELPERS ---
    async function loadRecent() {
        try {
            const res = await fetch('/recent');
            const data = await res.json();
            recentTableBody.innerHTML = '';
            data.items.forEach(row => {
                // CSV Order: Type, Desc, Serial, Tag
                const tr = document.createElement('tr');
                tr.innerHTML = `
                    <td>${row[0]}</td> <td>${row[1]}</td> <td>${row[2]}</td> `;
                recentTableBody.appendChild(tr);
            });
        } catch (err) {}
    }

    async function loadCompletedFiles() {
        try {
            const res = await fetch('/completed_files');
            const data = await res.json();
            fileListBody.innerHTML = '';
            data.files.forEach(f => {
                const tr = document.createElement('tr');
                tr.innerHTML = `
                    <td>${f}</td>
                    <td><a href="/download/${f}" class="btn btn-sm btn-outline-primary">Download</a></td>
                `;
                fileListBody.appendChild(tr);
            });
        } catch (err) {}
    }
});