package app.loho.nodeclient.test;

import android.app.Activity;
import android.app.Instrumentation;
import android.content.Intent;
import android.os.Bundle;
import android.os.SystemClock;
import android.view.InputDevice;
import android.view.MotionEvent;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.widget.LinearLayout;
import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/** Runs actual touchscreen gestures against the production Activity's WebView. */
public final class ZoomCheck extends Instrumentation {
    private Activity activity;
    private WebView browser;
    private final int[] location = new int[2];
    private int width, height;
    private LinearLayout root;
    private static final String PAGE = "<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'>"
        + "<style>body{margin:0;font:16px sans-serif}button,input{font:inherit;height:48px}button{width:96px}main{height:2400px;background:#d7e5df}</style>"
        + "<button onclick='window.taps=(window.taps||0)+1'>Tap</button><input placeholder='Input'><main>Gesture target</main>";

    @Override public void onCreate(Bundle args) { super.onCreate(args); start(); }
    @Override public void onStart() {
        Bundle result = new Bundle();
        try {
            Intent launch = new Intent().setClassName("app.loho.nodeclient", "app.loho.nodeclient.MainActivity")
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            activity = startActivitySync(launch);
            runOnMainSync(() -> {
                try {
                    Method create = activity.getClass().getDeclaredMethod("createBrowser"); create.setAccessible(true); create.invoke(activity);
                    Field viewField = activity.getClass().getDeclaredField("browser"); viewField.setAccessible(true);
                    browser = (WebView) viewField.get(activity);
                    Field rootField = activity.getClass().getDeclaredField("root"); rootField.setAccessible(true);
                    root = (LinearLayout) rootField.get(activity);
                    root.removeAllViews(); root.addView(browser, new LinearLayout.LayoutParams(-1, -1));
                    require(!browser.getSettings().supportZoom(), "Zoom support still enabled");
                    require(!browser.getSettings().getBuiltInZoomControls(), "Built-in zoom still enabled");
                    require(!browser.getSettings().getDisplayZoomControls(), "Zoom buttons still enabled");
                } catch (Exception e) { throw new RuntimeException(e); }
            });
            load();
            runOnMainSync(() -> { browser.getLocationOnScreen(location); width = browser.getWidth(); height = browser.getHeight(); });
            double initial = number("visualViewport.scale");
            pinch();
            require(Math.abs(number("visualViewport.scale") - initial) < .02, "Pinch changed app scale");
            tap(location[0] + width * .5f, location[1] + height * .35f);
            SystemClock.sleep(70);
            tap(location[0] + width * .5f, location[1] + height * .35f);
            SystemClock.sleep(600);
            require(Math.abs(number("visualViewport.scale") - initial) < .02, "Double tap changed app scale");
            tap(location[0] + width * .06f, location[1] + 24 * getTargetContext().getResources().getDisplayMetrics().density);
            SystemClock.sleep(300);
            require(number("window.taps||0") == 1, "Ordinary button tap failed");
            swipe();
            require(number("scrollY") > 50, "Ordinary scrolling failed");

            // Even a provider that enables zoom must not receive two-finger input.
            runOnMainSync(() -> { browser.getSettings().setSupportZoom(true); browser.getSettings().setBuiltInZoomControls(true); });
            load(); pinch();
            require(Math.abs(number("visualViewport.scale") - initial) < .02, "Native multi-touch guard failed with provider zoom enabled");

            checkRealClient();

            // Positive control: prove that the injected two-finger gesture can zoom.
            runOnMainSync(() -> {
                browser = new WebView(activity);browser.getSettings().setJavaScriptEnabled(true);
                browser.getSettings().setSupportZoom(true);browser.getSettings().setBuiltInZoomControls(true);
                root.removeAllViews();root.addView(browser,new LinearLayout.LayoutParams(-1,-1));
            });
            load();
            pinch();
            double control = number("visualViewport.scale");
            require(control > initial + .05, "Positive control did not zoom: " + control);
            result.putString("stream", "LOHO_ZOOM_PASS: flat page, four real client iframe tabs and user dialog stay at scale 1; provider-enabled guard passes; tap/scroll work; plain-WebView control scale=" + control + "\n");
            runOnMainSync(() -> {browser.destroy();activity.finish();});
            finish(Activity.RESULT_OK, result);
        } catch (Throwable error) {
            result.putString("stream", "LOHO_ZOOM_FAIL: " + android.util.Log.getStackTraceString(error));
            finish(Activity.RESULT_CANCELED, result);
        }
    }
    private static void require(boolean condition, String message) { if (!condition) throw new AssertionError(message); }
    private void checkRealClient() throws Exception {
        runOnMainSync(() -> {
            browser.setWebViewClient(new WebViewClient(){
                @Override public WebResourceResponse shouldInterceptRequest(WebView view,WebResourceRequest request){
                    try {
                        String path=request.getUrl().getPath(),value=null;
                        if(path.endsWith("/manager/api/users"))value="{\"users\":[{\"key\":\"demo-0\",\"label\":\"ADMIN\",\"enabled\":true,\"masked\":\"test\"}],\"csrf\":\"fixture\",\"revision\":\"qa\",\"busy\":false}";
                        else if(path.endsWith("/manager/api/profiles"))value="{\"admin\":{\"label\":\"demo-admin\",\"url\":\"\",\"version\":\"\"},\"users\":{},\"csrf\":\"fixture\"}";
                        if(value!=null)return new WebResourceResponse("application/json","UTF-8",new ByteArrayInputStream(value.getBytes(StandardCharsets.UTF_8)));
                        if(!path.startsWith("/node-view/")||path.contains(".."))throw new IllegalArgumentException("Unexpected test path");
                        String name=path.substring("/node-view/".length());
                        String mime=name.endsWith(".html")?"text/html":name.endsWith(".js")?"application/javascript":name.endsWith(".css")?"text/css":name.endsWith(".png")?"image/png":name.endsWith(".svg")?"image/svg+xml":"application/json";
                        return new WebResourceResponse(mime,"UTF-8",getContext().getAssets().open("panel/"+name));
                    }catch(Exception e){return new WebResourceResponse("text/plain","UTF-8",404,"Not found",null,new ByteArrayInputStream(new byte[0]));}
                }
            });
            browser.loadUrl("https://loho.invalid/node-view/client.html");
        });
        waitFor("!!window.NodeViewClientHost && !!document.querySelector('.client-nav')");
        String[] routes={"overview","connections","users","notifications"};
        String[] selectors={".user-card",".record",".account","#topic"};
        for(int i=0;i<routes.length;i++){
            js("NodeViewClientHost.navigate('/node-view/client.html?tab="+routes[i]+"')");
            waitFor("!!document.querySelector('iframe:not([hidden])')?.contentDocument?.querySelector('"+selectors[i]+"')");
            // Remove the CSS fallback so this specifically proves the native guard.
            js("document.documentElement.style.setProperty('touch-action','auto','important');document.querySelector('iframe:not([hidden])').contentDocument.documentElement.style.setProperty('touch-action','auto','important')");
            pinch();
            require(Math.abs(number("visualViewport.scale")-1)<.02,"Root zoomed on "+routes[i]);
            require(Math.abs(number("document.querySelector('iframe:not([hidden])').contentWindow.visualViewport.scale")-1)<.02,"Iframe zoomed on "+routes[i]);
            if(i==1)require("true".equals(js("(()=>{const d=document.querySelector('iframe:not([hidden])').contentDocument,w=d.defaultView,s=['search','user','source','window'].map(id=>w.getComputedStyle(d.getElementById(id)));return s.every(x=>x.fontSize==='14px'&&x.fontWeight==='400')&&new Set(s.map(x=>x.fontFamily)).size===1})()")),"Android filter fonts differ");
            if(i==0){
                js("document.querySelector('iframe:not([hidden])').contentDocument.querySelector('.user-card').click()");pinch();
                require(Math.abs(number("visualViewport.scale")-1)<.02,"User dialog zoomed");
                js("document.querySelector('iframe:not([hidden])').contentDocument.querySelector('.profile-dialog').close()");
            }
        }
    }
    private void waitFor(String expression) throws Exception {
        for(int i=0;i<150;i++){if("true".equals(js(expression))){SystemClock.sleep(300);return;}SystemClock.sleep(100);}
        throw new AssertionError("Client did not become ready: "+expression);
    }
    private void load() throws Exception {
        runOnMainSync(() -> browser.loadDataWithBaseURL("https://loho.invalid/node-view/client.html", PAGE, "text/html", "UTF-8", null));
        for (int i = 0; i < 80; i++) {
            if ("true".equals(js("document.readyState==='complete' && !!document.querySelector('main')"))) { SystemClock.sleep(600); return; }
            SystemClock.sleep(100);
        }
        throw new AssertionError("Test page did not load");
    }
    private String js(String expression) throws Exception {
        AtomicReference<String> value = new AtomicReference<>(); CountDownLatch ready = new CountDownLatch(1);
        runOnMainSync(() -> browser.evaluateJavascript(expression, result -> { value.set(result); ready.countDown(); }));
        require(ready.await(5, TimeUnit.SECONDS), "JavaScript timeout"); return value.get();
    }
    private double number(String expression) throws Exception { return Double.parseDouble(js(expression)); }
    private void event(long start, int action, float[] xs, float[] ys) {
        MotionEvent.PointerProperties[] properties = new MotionEvent.PointerProperties[xs.length];
        MotionEvent.PointerCoords[] coords = new MotionEvent.PointerCoords[xs.length];
        for (int i = 0; i < xs.length; i++) {
            properties[i] = new MotionEvent.PointerProperties(); properties[i].id = i; properties[i].toolType = MotionEvent.TOOL_TYPE_FINGER;
            coords[i] = new MotionEvent.PointerCoords(); coords[i].x = xs[i]; coords[i].y = ys[i]; coords[i].pressure = 1; coords[i].size = 1;
        }
        MotionEvent motion = MotionEvent.obtain(start, SystemClock.uptimeMillis(), action, xs.length, properties, coords,
            0, 0, 1, 1, 0, 0, InputDevice.SOURCE_TOUCHSCREEN, 0);
        try { require(getUiAutomation().injectInputEvent(motion, true), "Touch injection failed"); } finally { motion.recycle(); }
    }
    private void tap(float x, float y) {
        long start = SystemClock.uptimeMillis(); event(start, MotionEvent.ACTION_DOWN, new float[]{x}, new float[]{y});
        SystemClock.sleep(50); event(start, MotionEvent.ACTION_UP, new float[]{x}, new float[]{y});
    }
    private void pinch() {
        float x = location[0] + width * .5f, y = location[1] + height * .45f;
        float radius = width * .08f; long start = SystemClock.uptimeMillis();
        event(start, MotionEvent.ACTION_DOWN, new float[]{x - radius}, new float[]{y});
        event(start, MotionEvent.ACTION_POINTER_DOWN | (1 << MotionEvent.ACTION_POINTER_INDEX_SHIFT), new float[]{x - radius, x + radius}, new float[]{y, y});
        for (int i = 1; i <= 24; i++) {
            radius = width * (.08f + .27f * i / 24); SystemClock.sleep(20);
            event(start, MotionEvent.ACTION_MOVE, new float[]{x - radius, x + radius}, new float[]{y, y});
        }
        event(start, MotionEvent.ACTION_POINTER_UP | (1 << MotionEvent.ACTION_POINTER_INDEX_SHIFT), new float[]{x - radius, x + radius}, new float[]{y, y});
        event(start, MotionEvent.ACTION_UP, new float[]{x - radius}, new float[]{y}); SystemClock.sleep(600);
    }
    private void swipe() {
        float x = location[0] + width * .5f, y = location[1] + height * .75f; long start = SystemClock.uptimeMillis();
        event(start, MotionEvent.ACTION_DOWN, new float[]{x}, new float[]{y});
        for (int i = 1; i <= 20; i++) {
            SystemClock.sleep(16); event(start, MotionEvent.ACTION_MOVE, new float[]{x}, new float[]{y - height * .5f * i / 20});
        }
        event(start, MotionEvent.ACTION_UP, new float[]{x}, new float[]{y - height * .5f}); SystemClock.sleep(500);
    }
}
