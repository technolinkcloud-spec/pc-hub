/* === Virtual On-Screen Keyboard === */
(function() {
    'use strict';

    let activeInput = null;
    let shifted = false;
    let capsLock = false;
    let keyboardEl = null;

    const ROWS = [
        ['https://','http://','touchScreen?','graphicalDisplay?','unitId=','Intro17','Cinematic'],
        ['`','1','2','3','4','5','6','7','8','9','0','-','=','Backspace'],
        ['Tab','q','w','e','r','t','y','u','i','o','p','[',']','\\'],
        ['Caps','a','s','d','f','g','h','j','k','l',';','\'','Enter'],
        ['Shift','z','x','c','v','b','n','m',',','.','/',  '?','Shift'],
        ['Space']
    ];

    const SHIFT_MAP = {
        '`':'~','1':'!','2':'@','3':'#','4':'$','5':'%','6':'^','7':'&','8':'*','9':'(','0':')',
        '-':'_','=':'+','[':'{',']':'}','\\':'|',';':':','\'':'"',',':'<','.':'>','/':'?'
    };

    function createKeyboard() {
        if (keyboardEl) return;

        keyboardEl = document.createElement('div');
        keyboardEl.id = 'virtual-keyboard';
        keyboardEl.style.cssText = `
            position:fixed;bottom:0;left:0;right:0;z-index:3000;
            background:#1a1d27;border-top:2px solid #2e3148;padding:8px;
            display:none;box-shadow:0 -4px 24px rgba(0,0,0,0.5);
            transition:transform 0.2s ease;
        `;

        const closeBtn = document.createElement('button');
        closeBtn.textContent = '✕';
        closeBtn.style.cssText = `
            position:absolute;top:4px;right:12px;background:none;border:none;
            color:#8b8fa8;font-size:18px;cursor:pointer;padding:4px 8px;z-index:1;
        `;
        closeBtn.addEventListener('click', hideKeyboard);
        keyboardEl.appendChild(closeBtn);

        ROWS.forEach(row => {
            const rowEl = document.createElement('div');
            rowEl.style.cssText = 'display:flex;gap:4px;margin-bottom:4px;justify-content:center;';

            row.forEach(key => {
                const btn = document.createElement('button');
                btn.className = 'vk-key';
                btn.dataset.key = key;
                btn.textContent = key;

                let flex = '1';
                let minW = '36px';
                const isShortcut = key.length > 1 && !['Backspace','Enter','Tab','Space','Caps','Shift'].includes(key);
                if (key === 'Space') { flex = '8'; minW = '200px'; btn.textContent = ''; }
                else if (key === 'Backspace') { flex = '2'; minW = '70px'; }
                else if (key === 'Enter') { flex = '2'; minW = '70px'; }
                else if (key === 'Tab') { flex = '1.5'; minW = '55px'; }
                else if (key === 'Caps') { flex = '1.8'; minW = '65px'; }
                else if (key === 'Shift') { flex = '2.2'; minW = '80px'; }
                else if (isShortcut) { flex = '1'; minW = 'auto'; }

                const maxW = isShortcut ? '180px' : '120px';
                const fontSize = isShortcut ? '11px' : '14px';
                const pad = isShortcut ? '0 8px' : '0';
                const bg = isShortcut ? '#2a3050' : '#242736';

                btn.style.cssText = `
                    flex:${flex};min-width:${minW};max-width:${maxW};height:42px;
                    background:${bg};border:1px solid #2e3148;border-radius:6px;
                    color:#e4e6f0;font-size:${fontSize};font-family:inherit;cursor:pointer;
                    display:flex;align-items:center;justify-content:center;padding:${pad};
                    transition:background 0.1s;user-select:none;white-space:nowrap;
                `;

                btn.addEventListener('mousedown', (e) => {
                    e.preventDefault();
                    handleKey(key);
                });

                btn.addEventListener('touchstart', (e) => {
                    e.preventDefault();
                    handleKey(key);
                }, { passive: false });

                rowEl.appendChild(btn);
            });

            keyboardEl.appendChild(rowEl);
        });

        document.body.appendChild(keyboardEl);

        // Add hover styles
        const style = document.createElement('style');
        style.textContent = `
            .vk-key:hover,.vk-key:active{background:#3a3d50!important;}
            .vk-key.active-key{background:#5b6abf!important;color:#fff!important;}
        `;
        document.head.appendChild(style);
    }

    function handleKey(key) {
        if (!activeInput) return;

        const start = activeInput.selectionStart;
        const end = activeInput.selectionEnd;
        const val = activeInput.value;

        if (key === 'Backspace') {
            if (start !== end) {
                activeInput.value = val.slice(0, start) + val.slice(end);
                activeInput.selectionStart = activeInput.selectionEnd = start;
            } else if (start > 0) {
                activeInput.value = val.slice(0, start - 1) + val.slice(end);
                activeInput.selectionStart = activeInput.selectionEnd = start - 1;
            }
        } else if (key === 'Enter') {
            if (activeInput.tagName === 'TEXTAREA') {
                insertChar('\n');
            } else {
                const form = activeInput.closest('form');
                if (form) form.dispatchEvent(new Event('submit'));
                hideKeyboard();
            }
        } else if (key === 'Tab') {
            insertChar('\t');
        } else if (key === 'Space') {
            insertChar(' ');
        } else if (key === 'Caps') {
            capsLock = !capsLock;
            updateKeys();
        } else if (key === 'Shift') {
            shifted = !shifted;
            updateKeys();
        } else if (key.length > 1 && !['Backspace','Enter','Tab','Space','Caps','Shift'].includes(key)) {
            // Multi-character shortcut key — insert the whole string
            insertChar(key);
        } else {
            let ch = key;
            if (shifted || capsLock) {
                if (SHIFT_MAP[key]) {
                    ch = shifted ? SHIFT_MAP[key] : key;
                } else {
                    ch = (shifted !== capsLock) ? key.toUpperCase() : key.toLowerCase();
                }
            }
            insertChar(ch);
            if (shifted) {
                shifted = false;
                updateKeys();
            }
        }

        activeInput.dispatchEvent(new Event('input', { bubbles: true }));
    }

    function insertChar(ch) {
        if (!activeInput) return;
        const start = activeInput.selectionStart;
        const end = activeInput.selectionEnd;
        const val = activeInput.value;
        activeInput.value = val.slice(0, start) + ch + val.slice(end);
        activeInput.selectionStart = activeInput.selectionEnd = start + ch.length;
    }

    function updateKeys() {
        if (!keyboardEl) return;
        keyboardEl.querySelectorAll('.vk-key').forEach(btn => {
            const key = btn.dataset.key;
            if (key === 'Caps') {
                btn.classList.toggle('active-key', capsLock);
            } else if (key === 'Shift') {
                btn.classList.toggle('active-key', shifted);
            } else if (key.length === 1) {
                if (shifted && SHIFT_MAP[key]) {
                    btn.textContent = SHIFT_MAP[key];
                } else if (shifted !== capsLock && key.match(/[a-z]/)) {
                    btn.textContent = key.toUpperCase();
                } else {
                    btn.textContent = key;
                }
            }
        });
    }

    function showKeyboard() {
        createKeyboard();
        keyboardEl.style.display = '';
        scrollInputIntoView();
    }

    function hideKeyboard() {
        if (keyboardEl) keyboardEl.style.display = 'none';
        activeInput = null;
        document.documentElement.style.scrollPaddingBottom = '';
        document.body.style.paddingBottom = '';
    }

    function scrollInputIntoView() {
        if (!activeInput || !keyboardEl) return;
        // Wait one frame so the keyboard is rendered and has a measurable height
        requestAnimationFrame(function() {
            const kbHeight = keyboardEl.offsetHeight;
            // Add REAL space at the bottom of the page. scroll-padding alone
            // can't lift an input that sits at the very end of the page — there
            // has to be something below it to scroll into. This padding gives
            // the page room to scroll the focused field above the keyboard.
            document.body.style.paddingBottom = (kbHeight + 24) + 'px';

            const margin = 16;                                  // breathing room
            const kbTop = window.innerHeight - kbHeight;        // keyboard's top edge
            const rect = activeInput.getBoundingClientRect();
            // If the field is behind (or close to) the keyboard, scroll it up
            // until its bottom sits just above the keyboard.
            if (rect.bottom > kbTop - margin) {
                window.scrollBy({ top: rect.bottom - (kbTop - margin), behavior: 'smooth' });
            }
        });
    }

    function isTypableInput(el) {
        if (!el) return false;
        const tag = el.tagName;
        const type = (el.type || '').toLowerCase();
        if (tag === 'TEXTAREA') return true;
        if (tag === 'INPUT' && !['checkbox','radio','submit','button','file','hidden','range','color'].includes(type)) return true;
        return false;
    }

    // Attach to all inputs and textareas
    document.addEventListener('focusin', (e) => {
        if (isTypableInput(e.target)) {
            activeInput = e.target;
            showKeyboard();
        }
    });

    // Click on a label → focus the associated input and show keyboard
    document.addEventListener('click', (e) => {
        const label = e.target.closest('label, .form-label');
        if (!label) return;

        // If label has a "for" attribute, use that
        if (label.htmlFor) {
            const input = document.getElementById(label.htmlFor);
            if (input && isTypableInput(input)) {
                input.focus();
                return;
            }
        }

        // Otherwise find the nearest input in the same parent container
        const container = label.closest('.form-group, .form-inline, .form-row') || label.parentElement;
        if (container) {
            const input = container.querySelector('input:not([type=checkbox]):not([type=radio]):not([type=hidden]):not([type=submit]):not([type=button]), textarea');
            if (input && isTypableInput(input)) {
                input.focus();
            }
        }
    });

    // Hide keyboard when clicking outside inputs and keyboard
    document.addEventListener('focusout', (e) => {
        setTimeout(() => {
            const active = document.activeElement;
            if (isTypableInput(active)) return;
            if (keyboardEl && keyboardEl.contains(active)) return;
            hideKeyboard();
        }, 200);
    });

})();
