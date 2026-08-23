/*
 * speech-to-text.js
 * ==================
 * Free, browser-based speech-to-text using the Web Speech API.
 * No API key, no backend calls, no cost. Works in Chrome / Edge (desktop & Android).
 * NOT supported in Firefox or Safari - the mic button auto-hides there.
 *
 * USAGE - add this to create_complaint.html (or any page with a textarea you want
 * voice input for):
 *
 *   1. Add a mic button next to the textarea:
 *        <button type="button" id="mic-btn-description" class="btn btn-outline-secondary speech-mic-btn">
 *            🎤 Speak
 *        </button>
 *
 *   2. Include this script and initialize it, pointing at your textarea's id:
 *        <script src="{{ url_for('static', filename='js/speech-to-text.js') }}"></script>
 *        <script>
 *            initSpeechToText('mic-btn-description', 'description');
 *        </script>
 *
 *      ('description' should match the id of your ComplaintForm's description
 *      textarea/input - check forms.py / the rendered HTML if unsure.)
 *
 * You can call initSpeechToText() multiple times for multiple fields
 * (e.g. one for "title", one for "description").
 */

function initSpeechToText(buttonId, targetFieldId) {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    const button = document.getElementById(buttonId);
    const field = document.getElementById(targetFieldId);

    if (!button || !field) {
        console.warn(`speech-to-text: could not find #${buttonId} or #${targetFieldId}`);
        return;
    }

    if (!SpeechRecognition) {
        // Browser doesn't support it (Firefox/Safari) - hide the button rather than show a dead control.
        button.style.display = 'none';
        console.info('speech-to-text: Web Speech API not supported in this browser.');
        return;
    }

    const recognition = new SpeechRecognition();
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.lang = 'en-IN'; // change to 'en-US' or another locale if preferred

    let listening = false;
    const originalLabel = button.innerHTML;

    recognition.onstart = () => {
        listening = true;
        button.innerHTML = '🔴 Listening... (click to stop)';
        button.classList.add('listening');
    };

    recognition.onend = () => {
        listening = false;
        button.innerHTML = originalLabel;
        button.classList.remove('listening');
    };

    recognition.onerror = (event) => {
        console.error('speech-to-text error:', event.error);
        listening = false;
        button.innerHTML = originalLabel;
        button.classList.remove('listening');
        if (event.error === 'not-allowed') {
            alert('Microphone access was denied. Please allow microphone permission to use voice input.');
        }
    };

    recognition.onresult = (event) => {
        let transcript = '';
        for (let i = 0; i < event.results.length; i++) {
            transcript += event.results[i][0].transcript;
        }
        // Append to existing text rather than overwrite, with a space separator
        const existing = field.value.trim();
        field.value = existing ? `${existing} ${transcript}` : transcript;
    };

    button.addEventListener('click', () => {
        if (listening) {
            recognition.stop();
        } else {
            recognition.start();
        }
    });
}