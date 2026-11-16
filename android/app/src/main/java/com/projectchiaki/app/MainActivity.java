package com.projectchiaki.app;

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Environment;
import android.provider.Settings;
import android.net.Uri;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.system.Os;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.webkit.CookieManager;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.SocketTimeoutException;
import java.util.ArrayList;
import java.util.List;

/**
 * Project Chiaki on Android. The start screen offers two things:
 *  - Play on this phone: the emulator (Python, through Chaquopy) serves its page on this phone only (127.0.0.1)
 *    and this window shows it full screen, the same page as the Windows program's window.
 *  - Smart link: this phone becomes the remote control and song search of Project Chiaki running on a computer on
 *    the same Wi-Fi (Settings > Smart link there). Computers are found with a broadcast ("CHIAKI?" on UDP 8771);
 *    an address can also be typed. The emulator does not start in this mode.
 *
 * The user's files (Play here) live in the app's folder on the phone's storage:
 *   Android/data/com.projectchiaki.app/files/  BIOS, Discs, Soundfont, BGV, Saved Settings, Logs
 * and, optionally, the file "ffmpeg" (Android ARM64 build) next to those folders for pictures and video.
 */
public class MainActivity extends Activity {
    private static final int UDP_PORT = 8771;
    private FrameLayout root;
    private WebView web;
    private TextView status;
    private long backAt = 0;
    private boolean backChecked = false;
    private boolean inWeb = false;
    private SharedPreferences prefs;
    private final Handler ui = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        prefs = getSharedPreferences("chiaki", MODE_PRIVATE);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        if (Build.VERSION.SDK_INT >= 28) {
            getWindow().getAttributes().layoutInDisplayCutoutMode =
                    WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
        }
        root = new FrameLayout(this);
        root.setBackgroundColor(Color.rgb(21, 23, 28));
        setContentView(root);
        hideBars();
        showStart();
    }

    // ---------------------------------------------------------------- start screen
    private Button bigButton(String text, int color) {
        Button b = new Button(this);
        b.setText(text);
        b.setAllCaps(false);
        b.setTextColor(Color.WHITE);
        b.setTextSize(18);
        GradientDrawable g = new GradientDrawable();
        g.setColor(color);
        g.setCornerRadius(36);
        b.setBackground(g);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(-1, dp(64));
        lp.topMargin = dp(12);
        b.setLayoutParams(lp);
        return b;
    }

    private int dp(int v) {
        return (int) (v * getResources().getDisplayMetrics().density);
    }

    private LinearLayout column() {
        root.removeAllViews();
        ScrollView sv = new ScrollView(this);
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setPadding(dp(28), dp(28), dp(28), dp(28));
        col.setGravity(Gravity.CENTER_HORIZONTAL);
        FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(Math.min(dp(460), getResources().getDisplayMetrics().widthPixels), -2);
        lp.gravity = Gravity.CENTER;
        sv.addView(col);
        root.addView(sv, lp);
        TextView t = new TextView(this);
        t.setText("PROJECT CHIAKI");
        t.setTextColor(Color.rgb(255, 155, 203));
        t.setTextSize(22);
        t.setLetterSpacing(0.2f);
        t.setGravity(Gravity.CENTER);
        col.addView(t);
        return col;
    }

    private TextView note(LinearLayout col, String text) {
        TextView n = new TextView(this);
        n.setText(text);
        n.setTextColor(Color.rgb(139, 149, 169));
        n.setTextSize(14);
        n.setGravity(Gravity.CENTER);
        n.setPadding(0, dp(10), 0, 0);
        col.addView(n);
        return n;
    }

    private void showStart() {
        inWeb = false;
        LinearLayout col = column();
        Button here = bigButton("▶  Play on this phone", Color.rgb(255, 79, 163));
        here.setOnClickListener(v -> startHere());
        col.addView(here);
        TextView where = note(col, "Files: " + placeName(prefs.getString("place", "internal")) + " › Project Chiaki"
                + "  (BIOS, Discs, Soundfont, BGV, ffmpeg)");
        Button place = bigButton("📁  Use " + placeName(prefs.getString("place", "internal").equals("sd") ? "internal" : "sd") + " instead", Color.rgb(36, 40, 50));
        place.setOnClickListener(v -> {
            String now = prefs.getString("place", "internal");
            if (now.equals("internal") && sdRoot() == null) {
                Toast.makeText(this, "No SD card found", Toast.LENGTH_SHORT).show();
                return;
            }
            prefs.edit().putString("place", now.equals("sd") ? "internal" : "sd").apply();
            showStart();
        });
        col.addView(place);
        Button link = bigButton("📶  Smart link to a computer", Color.rgb(48, 54, 68));
        link.setOnClickListener(v -> showSmartLink());
        col.addView(link);
        note(col, "This phone becomes the remote and song search for Project Chiaki on your computer (same Wi-Fi).");
        String last = prefs.getString("last_url", "");
        if (!last.isEmpty()) {
            Button again = bigButton("↻  " + prefs.getString("last_name", "Last computer") + "  ·  " + hostOf(last), Color.rgb(48, 54, 68));
            again.setOnClickListener(v -> openRemote(last, prefs.getString("last_name", "")));
            col.addView(again);
        }
    }

    private static String hostOf(String url) {
        String h = url.replace("http://", "");
        int i = h.indexOf('/');
        return i >= 0 ? h.substring(0, i) : h;
    }

    // ---------------------------------------------------------------- smart link
    private void showSmartLink() {
        inWeb = false;
        LinearLayout col = column();
        TextView how = note(col, "On the computer: Project Chiaki ⚙ Settings → 📶 Smart link → tick the box. Then pick it below and type its PIN.");
        how.setTextColor(Color.rgb(232, 236, 244));
        LinearLayout found = new LinearLayout(this);
        found.setOrientation(LinearLayout.VERTICAL);
        col.addView(found);
        TextView st = note(col, "Searching the Wi-Fi…");
        Button again = bigButton("🔎  Search again", Color.rgb(48, 54, 68));
        col.addView(again);
        EditText addr = new EditText(this);
        addr.setHint("or type the address, e.g. 192.168.1.20:8770");
        addr.setTextColor(Color.WHITE);
        addr.setHintTextColor(Color.rgb(110, 118, 135));
        addr.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        addr.setText(hostOf(prefs.getString("last_url", "")));
        LinearLayout.LayoutParams alp = new LinearLayout.LayoutParams(-1, -2);
        alp.topMargin = dp(18);
        col.addView(addr, alp);
        Button go = bigButton("Connect", Color.rgb(255, 79, 163));
        go.setOnClickListener(v -> {
            String a = addr.getText().toString().trim();
            if (a.isEmpty()) return;
            if (!a.startsWith("http")) a = "http://" + a;
            if (!a.substring(7).contains(":")) a = a + ":8770";
            if (!a.endsWith("/")) a = a + "/";
            openRemote(a, "");
        });
        col.addView(go);
        Button back = bigButton("←  Back", Color.rgb(36, 40, 50));
        back.setOnClickListener(v -> showStart());
        col.addView(back);
        Runnable search = () -> {
            st.setText("Searching the Wi-Fi…");
            found.removeAllViews();
            new Thread(() -> {
                List<String[]> hits = discover();
                ui.post(() -> {
                    if (!inWeb) {
                        found.removeAllViews();
                        for (String[] h : hits) {
                            Button b = bigButton("💻  " + h[1] + "  ·  " + hostOf(h[0]), Color.rgb(64, 72, 92));
                            b.setOnClickListener(v -> openRemote(h[0], h[1]));
                            found.addView(b);
                        }
                        st.setText(hits.isEmpty() ? "No computer found. Is Smart link ticked there, and is this phone on the same Wi-Fi?" : "");
                    }
                });
            }, "chiaki-find").start();
        };
        again.setOnClickListener(v -> search.run());
        search.run();
    }

    /** Broadcasts "CHIAKI?" on the Wi-Fi and collects the answers: [url, computer name]. */
    private List<String[]> discover() {
        List<String[]> out = new ArrayList<>();
        try (DatagramSocket s = new DatagramSocket()) {
            s.setBroadcast(true);
            s.setSoTimeout(400);
            byte[] q = "CHIAKI?".getBytes("UTF-8");
            for (int round = 0; round < 3; round++) {
                s.send(new DatagramPacket(q, q.length, InetAddress.getByName("255.255.255.255"), UDP_PORT));
                long until = System.currentTimeMillis() + 600;
                while (System.currentTimeMillis() < until) {
                    byte[] buf = new byte[512];
                    DatagramPacket p = new DatagramPacket(buf, buf.length);
                    try {
                        s.receive(p);
                    } catch (SocketTimeoutException e) {
                        break;
                    }
                    String r = new String(p.getData(), 0, p.getLength(), "UTF-8");
                    if (!r.startsWith("CHIAKI ")) continue;
                    String[] parts = r.substring(7).split(" ", 2);
                    // the computer may report an address the phone cannot reach (several network cards): use the
                    // one the answer came from, with the port it reported
                    String url = parts[0];
                    String port = url.replaceAll("^http://[^:/]+:?(\\d*)/?$", "$1");
                    url = "http://" + p.getAddress().getHostAddress() + ":" + (port.isEmpty() ? "8770" : port) + "/";
                    String name = parts.length > 1 ? parts[1] : "Computer";
                    boolean dup = false;
                    for (String[] h : out) if (h[0].equals(url)) dup = true;
                    if (!dup) out.add(new String[]{url, name});
                }
            }
        } catch (Exception ignored) {
        }
        return out;
    }

    private void openRemote(String url, String name) {
        prefs.edit().putString("last_url", url).putString("last_name", name.isEmpty() ? "Computer" : name).apply();
        CookieManager.getInstance().setAcceptCookie(true);
        showWeb(url);
    }

    // ---------------------------------------------------------------- where the files are
    private static String placeName(String place) {
        return place.equals("sd") ? "SD card" : "Internal storage";
    }

    /** The SD card's top folder (e.g. /storage/1A2B-3C4D), or null without one. */
    private File sdRoot() {
        File[] dirs = getExternalFilesDirs(null);
        for (int i = 1; dirs != null && i < dirs.length; i++) {
            if (dirs[i] == null) continue;
            String p = dirs[i].getAbsolutePath();
            int k = p.indexOf("/Android/data/");
            if (k > 0) return new File(p.substring(0, k));
        }
        return null;
    }

    private boolean canWrite(File dir) {
        try {
            if (!dir.isDirectory() && !dir.mkdirs()) return false;
            File t = new File(dir, ".chiaki-test");
            try (FileOutputStream o = new FileOutputStream(t)) {
                o.write(1);
            }
            return t.delete();
        } catch (Exception e) {
            return false;
        }
    }

    /** The data folder: "Project Chiaki" at the top of the chosen storage. An SD card's top folder cannot always
     *  be written to (Android allows it only to some apps): then the app's own folder on the card. */
    private File home() {
        boolean sd = prefs.getString("place", "internal").equals("sd");
        File root = sd ? sdRoot() : Environment.getExternalStorageDirectory();
        if (root != null) {
            File h = new File(root, "Project Chiaki");
            if (canWrite(h)) return h;
        }
        if (sd) {
            File[] dirs = getExternalFilesDirs(null);
            if (dirs != null && dirs.length > 1 && dirs[1] != null && canWrite(dirs[1])) return dirs[1];
        }
        File h = getExternalFilesDir(null);
        return h != null ? h : getFilesDir();
    }

    private static final int REQ_FILES = 7;

    /** Storage permission: "All files access" on Android 11 and later, the storage permission before. */
    private boolean askFiles() {
        if (Build.VERSION.SDK_INT >= 30) {
            if (Environment.isExternalStorageManager()) return true;
            Toast.makeText(this, "Allow \"All files access\" for Project Chiaki, then go back", Toast.LENGTH_LONG).show();
            try {
                startActivity(new Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION, Uri.parse("package:" + getPackageName())));
            } catch (Exception e) {
                startActivity(new Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION));
            }
            pendingStart = true;
            return false;
        }
        if (Build.VERSION.SDK_INT >= 23 && checkSelfPermission(android.Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{android.Manifest.permission.READ_EXTERNAL_STORAGE, android.Manifest.permission.WRITE_EXTERNAL_STORAGE}, REQ_FILES);
            return false;
        }
        return true;
    }

    private boolean pendingStart = false;

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] res) {
        super.onRequestPermissionsResult(code, perms, res);
        if (code == REQ_FILES) startHere(true);      // (without the permission: the app's own folder)
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (pendingStart) {
            pendingStart = false;
            startHere(true);
        }
    }

    // ---------------------------------------------------------------- play here
    private void startHere() {
        startHere(false);
    }

    private void startHere(boolean asked) {
        if (!asked && !askFiles()) return;
        showWeb(null);
        final File homeF = home();
        status.setText("Project Chiaki\nstarting…\n\nFiles: " + homeF.getAbsolutePath().replace("/storage/emulated/0", "Internal storage"));
        new Thread(() -> start(homeF), "chiaki-start").start();
    }

    private void start(File home) {
        try {
            // where Python finds the CPU core (libkecpu.so), and ffmpeg if the user put one in the folder
            Os.setenv("KE_LIBDIR", getApplicationInfo().nativeLibraryDir, true);
            File ff = installFfmpeg(home);
            if (ff != null) Os.setenv("KE_FFMPEG", ff.getAbsolutePath(), true);

            if (!Python.isStarted()) Python.start(new AndroidPlatform(this));
            PyObject mod = Python.getInstance().getModule("karaoke_emu");
            final String url = mod.callAttr("android_start", home.getAbsolutePath()).toString();
            runOnUiThread(() -> web.loadUrl(url));
        } catch (Throwable t) {
            final String msg = "Project Chiaki could not start:\n\n" + t;
            runOnUiThread(() -> status.setText(msg));
        }
    }

    /** Copies "ffmpeg" from the user's folder into the app's private folder, where it may be run. */
    /** Copies the phone's ffmpeg from the user's folder into the app's private folder, where it may be run.
     *  Two builds exist: "ffmpeg" (ARM phones) and "ffmpeg-x86_64" (PC Android emulators such as LDPlayer, and x86
     *  tablets); either name may be used, and only one that runs on this device is taken. */
    private File installFfmpeg(File home) {
        String abi = Build.SUPPORTED_ABIS.length > 0 ? Build.SUPPORTED_ABIS[0] : "";
        String[] names = abi.startsWith("x86") ? new String[]{"ffmpeg-x86_64", "ffmpeg"} : new String[]{"ffmpeg", "ffmpeg-arm64"};
        for (String name : names) {
            File src = new File(home, name);
            if (!src.isFile()) continue;
            File dst = new File(getFilesDir(), "ffmpeg");
            try {
                if (!dst.isFile() || dst.length() != src.length() || dst.lastModified() < src.lastModified()) {
                    runOnUiThread(() -> status.setText("Project Chiaki\npreparing ffmpeg…"));
                    try (InputStream in = new FileInputStream(src); OutputStream out = new FileOutputStream(dst)) {
                        byte[] buf = new byte[1 << 20];
                        int n;
                        while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                    }
                }
                dst.setExecutable(true, true);
                if (runs(dst)) return dst;
                dst.delete();
            } catch (Exception ignored) {
            }
        }
        return null;
    }

    private static boolean runs(File exe) {
        try {
            Process p = new ProcessBuilder(exe.getAbsolutePath(), "-version").redirectErrorStream(true).start();
            InputStream in = p.getInputStream();
            byte[] buf = new byte[4096];
            while (in.read(buf) > 0) { }
            return p.waitFor() == 0;
        } catch (Exception e) {
            return false;
        }
    }

    // ---------------------------------------------------------------- the page
    private void showWeb(String url) {
        inWeb = true;
        root.removeAllViews();
        web = new WebView(this);
        web.setBackgroundColor(Color.BLACK);
        web.setVisibility(View.INVISIBLE);
        status = new TextView(this);
        status.setTextColor(Color.rgb(200, 200, 210));
        status.setTextSize(16);
        status.setGravity(Gravity.CENTER);
        status.setPadding(48, 48, 48, 48);
        status.setText(url == null ? "Project Chiaki\nstarting…" : "Connecting to\n" + hostOf(url) + "…");
        root.addView(web, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        root.addView(status, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        WebSettings ws = web.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setMediaPlaybackRequiresUserGesture(false);
        ws.setLoadWithOverviewMode(true);
        ws.setUseWideViewPort(true);
        ws.setCacheMode(WebSettings.LOAD_NO_CACHE);
        ws.setTextZoom(100);           // the phone's font size setting made the page's buttons too big
        ws.setUserAgentString(ws.getUserAgentString() + " ProjectChiakiApp");   // the page: fullscreen without the browser API
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, String u) {
                // the app's own pages are plain http (this phone or the computer); web pages (the social links)
                // open in the phone's browser
                if (u != null && u.startsWith("https://")) {
                    try {
                        startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(u)));
                    } catch (Exception ignored) {
                    }
                    return true;
                }
                return false;
            }

            @Override
            public void onPageFinished(WebView v, String u) {
                web.setVisibility(View.VISIBLE);
                status.setVisibility(View.GONE);
            }

            @Override
            public void onReceivedError(WebView v, int code, String desc, String failing) {
                status.setVisibility(View.VISIBLE);
                status.setText("Could not reach " + hostOf(failing) + "\n\n" + desc
                        + "\n\nIs Project Chiaki running there with Smart link on, and this phone on the same Wi-Fi?\nPress Back to go back.");
                web.setVisibility(View.INVISIBLE);
            }
        });
        web.setWebChromeClient(new WebChromeClient());
        if (url != null) web.loadUrl(url);
    }

    private void hideBars() {
        getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY | View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN);
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) hideBars();
    }

    @Override
    public void onBackPressed() {
        if (inWeb && web != null && !backChecked) {
            // Back first closes what is open on the page: Adjust Display Area, a panel, then fullscreen
            web.evaluateJavascript("(function(){try{if(typeof CAL!=='undefined'&&CAL!==null){calibShow(false);return 1}"
                    + "var f=document.querySelector('.float.open');if(f){f.classList.remove('open');document.querySelectorAll('.fab button.on').forEach(function(b){b.classList.remove('on')});return 1}"
                    + "if(typeof PFS!=='undefined'&&PFS){pfs(false);return 1}}catch(e){}return 0})()", r -> {
                if (!"1".equals(r)) {
                    backChecked = true;
                    onBackPressed();
                    backChecked = false;
                }
            });
            return;
        }
        boolean emulatorRuns = Python.isStarted();
        if (inWeb && !emulatorRuns) {               // the remote of a computer: back to the start screen
            if (web != null) web.destroy();
            showStart();
            return;
        }
        if (!inWeb && !emulatorRuns) {
            finish();
            return;
        }
        long now = System.currentTimeMillis();
        if (now - backAt < 2000) {
            finish();
            // the emulator runs in this app's process: end it with the window, like closing the Windows program
            ui.postDelayed(() -> System.exit(0), 300);
        } else {
            backAt = now;
            Toast.makeText(this, "Press Back again to close Project Chiaki", Toast.LENGTH_SHORT).show();
        }
    }
}
