/* ── Ohara · Firebase configuration ─────────────────────────── */

const firebaseConfig = {
  apiKey:            "AIzaSyCg75hDTvNrMk3EHLTF0bFfIF1x6i-YtYQ",
  authDomain:        "ohara-reader-lib.firebaseapp.com",
  projectId:         "ohara-reader-lib",
  storageBucket:     "ohara-reader-lib.firebasestorage.app",
  messagingSenderId: "326707672774",
  appId:             "1:326707672774:web:4283f7515097122b3c09c7"
};

firebase.initializeApp(firebaseConfig);
const auth = firebase.auth();
