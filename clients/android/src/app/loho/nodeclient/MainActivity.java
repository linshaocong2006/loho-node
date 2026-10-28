package app.loho.nodeclient;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.view.WindowInsets;
import android.webkit.CookieManager;
import android.webkit.SslErrorHandler;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.webkit.RenderProcessGoneDetail;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import org.json.JSONArray;
import java.net.URI;
import java.util.ArrayList;
import java.util.List;

public final class MainActivity extends Activity {
    private WebView browser;
    private LinearLayout root;
    private SharedPreferences prefs;
    private Uri origin;
    private boolean showingError;
    private boolean resetHistory;
    private boolean backPending;
    private final int background = Color.rgb(16, 27, 24);
    private final int text = Color.rgb(228, 237, 223);

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        prefs = getSharedPreferences("servers", MODE_PRIVATE);
        root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(background);
        if (Build.VERSION.SDK_INT >= 30) {
            getWindow().setDecorFitsSystemWindows(false);
            root.setOnApplyWindowInsetsListener((view, insets) -> {
                android.graphics.Insets bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.ime());
                view.setPadding(bars.left, bars.top, bars.right, bars.bottom);
                return insets;
            });
        } else root.setFitsSystemWindows(true);
        setContentView(root);
        String last = prefs.getString("last", "");
        if (last.isEmpty()) showSetup(); else {
            try { connect(normalize(last)); } catch (Exception e) { showSetup(); }
        }
    }

    private int dp(int size) { return Math.round(getResources().getDisplayMetrics().density * size); }
    private TextView label(String value, int size) {
        TextView view = new TextView(this); view.setText(value); view.setTextColor(text);
        view.setTextSize(size); view.setPadding(0, dp(10), 0, dp(10)); return view;
    }
    private Button button(String value) { Button b = new Button(this); b.setText(value); return b; }
    private List<String> servers() {
        ArrayList<String> result = new ArrayList<>();
        try { JSONArray data = new JSONArray(prefs.getString("list", "[]"));
            for (int i = 0; i < data.length(); i++) result.add(data.getString(i));
        } catch (Exception ignored) { }
        return result;
    }
    public static String normalize(String input) throws Exception {
        String value = input.trim(); if (!value.contains("://")) value = "https://" + value;
        URI uri = new URI(value); String path = uri.getPath();
        if (!"https".equalsIgnoreCase(uri.getScheme()) || uri.getHost() == null || uri.getHost().isEmpty()
            || uri.getUserInfo() != null || uri.getQuery() != null || uri.getFragment() != null
            || uri.getPort() == 0 || uri.getPort() > 65535 || uri.getPort() < -1)
            throw new IllegalArgumentException("请填写 HTTPS 服务器地址，例如 https://你的域名:5000");
        if (path != null && !path.isEmpty() && !path.equals("/") && !path.equals("/node-view")
            && !path.equals("/node-view/") && !path.equals("/node-view/client.html"))
            throw new IllegalArgumentException("请填写服务器域名和端口，或 /node-view/ 面板地址。");
        return new URI("https", null, uri.getHost(), uri.getPort(), null, null, null).toASCIIString();
    }
    private void showSetup() {
        if (browser != null) { setAppPaused(true); browser.stopLoading(); browser.onPause(); }
        root.removeAllViews(); origin = null;
        ScrollView scroll = new ScrollView(this); LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL); box.setPadding(dp(24), dp(30), dp(24), dp(20));
        scroll.addView(box); root.addView(scroll, new LinearLayout.LayoutParams(-1, -1));
        box.addView(label("LoHo Node", 28));
        box.addView(label("连接你的服务器", 18));
        box.addView(label("首次使用请先升级服务器到 v10.0.0。网站账号和密码在服务器登录页输入，客户端不保存密码。", 13));
        EditText address = new EditText(this); address.setSingleLine(true); address.setTextColor(text);
        address.setHintTextColor(Color.rgb(140,160,145)); address.setHint("https://你的域名:5000");
        address.setInputType(android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_VARIATION_URI);
        box.addView(address, new LinearLayout.LayoutParams(-1, dp(58)));
        Button add = button("连接服务器"); box.addView(add);
        add.setOnClickListener(v -> { try { connect(normalize(address.getText().toString())); }
            catch (Exception e) { address.setError(e.getMessage()); } });
        List<String> saved = servers();
        if (!saved.isEmpty()) box.addView(label("已保存的服务器 · 长按可移除", 13));
        for (String server : saved) {
            Button choice = button(server); box.addView(choice);
            choice.setOnClickListener(v -> { try { connect(normalize(server)); } catch (Exception e) { showSetup(); } });
            choice.setOnLongClickListener(v -> { new AlertDialog.Builder(this).setTitle("移除服务器地址？")
                .setMessage("仅移除地址，不会删除服务器数据或退出已保存的登录。")
                .setNegativeButton("取消", null).setPositiveButton("移除", (d, w) -> {
                    List<String> remaining = servers(); remaining.remove(server);
                    SharedPreferences.Editor update = prefs.edit().putString("list", new JSONArray(remaining).toString());
                    if (server.equals(prefs.getString("last", ""))) update.remove("last");
                    update.apply(); showSetup();
                }).show(); return true; });
        }
    }
    private void connect(String address) {
        origin = Uri.parse(address); showingError = false; resetHistory = true;
        List<String> saved = servers(); if (!saved.contains(address)) saved.add(address);
        prefs.edit().putString("last", address).putString("list", new JSONArray(saved).toString()).apply();
        if (browser == null) createBrowser();
        root.removeAllViews(); root.addView(browser, new LinearLayout.LayoutParams(-1, -1));
        browser.onResume(); browser.resumeTimers(); browser.loadUrl(address + "/node-view/client.html");
    }
    private boolean sameOrigin(Uri target) {
        int left = origin.getPort() == -1 ? 443 : origin.getPort();
        int right = target.getPort() == -1 ? 443 : target.getPort();
        return "https".equalsIgnoreCase(target.getScheme()) && origin.getHost().equalsIgnoreCase(target.getHost())
            && left == right && target.getUserInfo() == null;
    }
    private void createBrowser() {
        browser = new WebView(this); browser.setBackgroundColor(background);
        WebSettings settings = browser.getSettings(); settings.setJavaScriptEnabled(true); settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false); settings.setAllowContentAccess(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setSupportMultipleWindows(false); settings.setSafeBrowsingEnabled(true);
        settings.setCacheMode(WebSettings.LOAD_DEFAULT);
        settings.setUserAgentString(settings.getUserAgentString() + " LoHoNodeAndroid/1.0.1");
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(browser, false);
        browser.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri target = request.getUrl();
                if ("lohonode".equals(target.getScheme()) && "settings".equals(target.getHost())) { showSetup(); return true; }
                if (origin != null && sameOrigin(target) && target.getPath() != null && ("/node-view".equals(target.getPath()) || target.getPath().startsWith("/node-view/"))) return false;
                if (request.hasGesture() && "https".equalsIgnoreCase(target.getScheme())) {
                    try { startActivity(new Intent(Intent.ACTION_VIEW, target)); } catch (Exception ignored) { }
                }
                return true;
            }
            @Override public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
                handler.cancel(); error("HTTPS 证书校验失败，请先修复服务器证书。客户端不会跳过证书校验。");
            }
            @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError value) {
                if (request.isForMainFrame()) error("暂时无法连接服务器，请检查地址、端口和网络，然后重试。");
            }
            @Override public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
                if (request.isForMainFrame() && response.getStatusCode() >= 400)
                    error(response.getStatusCode() == 404 ? "未找到客户端入口，请先把服务器升级到 v10.0.0。" : "服务器返回 HTTP " + response.getStatusCode() + "，请稍后重试。");
            }
            @Override public void onPageFinished(WebView view, String url) { if (resetHistory && origin != null && sameOrigin(Uri.parse(url))) { view.clearHistory(); resetHistory = false; } }
            @Override public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
                root.removeView(view); view.destroy(); if (browser == view) browser = null;
                error("页面运行已中断，点击重试即可重新打开。服务器数据不会丢失。"); return true;
            }
        });
    }
    private void error(String message) {
        if (showingError || isFinishing() || origin == null) return; showingError = true;
        new AlertDialog.Builder(this).setTitle("连接提示").setMessage(message)
            .setNegativeButton("服务器设置", (d, w) -> { showingError = false; showSetup(); })
            .setPositiveButton("重试", (d, w) -> { showingError = false; if (origin != null) connect(origin.toString()); })
            .setOnCancelListener(d -> showingError = false).show();
    }
    @Override public void onBackPressed() {
        if (browser != null && origin != null) {
            if (backPending) return; backPending = true;
            browser.evaluateJavascript("Boolean(window.NodeViewClientHost && window.NodeViewClientHost.back())", value -> {
                backPending = false; if (!"true".equals(value)) fallbackBack();
            }); return;
        }
        fallbackBack();
    }
    private void fallbackBack() {
        if (browser != null && origin != null) {
            android.webkit.WebBackForwardList history = browser.copyBackForwardList();
            for (int i = history.getCurrentIndex() - 1; i >= 0; i--) {
                Uri candidate = Uri.parse(history.getItemAtIndex(i).getUrl());
                if (sameOrigin(candidate) && candidate.getPath() != null && candidate.getPath().startsWith("/node-view/") && !candidate.getPath().endsWith("/login.html")) {
                    browser.goBackOrForward(i - history.getCurrentIndex()); return;
                }
            }
            showSetup();
        } else super.onBackPressed();
    }
    private void setAppPaused(boolean paused) {
        if (browser != null) browser.evaluateJavascript("window.NodeViewClientHost && window.NodeViewClientHost.setPaused(" + paused + ")", null);
    }
    @Override public void onPause() { super.onPause(); if (browser != null) { setAppPaused(true); browser.onPause(); browser.pauseTimers(); CookieManager.getInstance().flush(); } }
    @Override public void onResume() { super.onResume(); if (browser != null && origin != null) { browser.onResume(); browser.resumeTimers(); setAppPaused(false); } }
    @Override public void onDestroy() { if (browser != null) { root.removeView(browser); browser.destroy(); browser = null; } super.onDestroy(); }
}
