package app.loho.nodeclient.test;

import android.app.Activity;
import android.app.Instrumentation;
import android.content.Intent;
import android.os.Bundle;
import android.os.SystemClock;
import android.view.InputDevice;
import android.view.MotionEvent;
import android.view.KeyEvent;
import android.view.WindowInsets;
import android.graphics.Insets;
import android.graphics.Bitmap;
import android.net.Uri;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebChromeClient;
import android.webkit.ConsoleMessage;
import android.widget.LinearLayout;
import java.io.ByteArrayInputStream;
import java.io.File;
import java.io.FileOutputStream;
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
    private WebView appBrowser;
    private final int[] location = new int[2];
    private int width, height;
    private LinearLayout root;
    private volatile String diagnostics="";
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
                    appBrowser = browser;
                    Field originField=activity.getClass().getDeclaredField("origin");originField.setAccessible(true);originField.set(activity,Uri.parse("https://loho.invalid"));
                    Field rootField = activity.getClass().getDeclaredField("root"); rootField.setAccessible(true);
                    root = (LinearLayout) rootField.get(activity);
                    root.removeAllViews(); root.addView(browser, new LinearLayout.LayoutParams(-1, -1));
                    require(!browser.getSettings().supportZoom(), "Zoom support still enabled");
                    require(!browser.getSettings().getBuiltInZoomControls(), "Built-in zoom still enabled");
                    require(!browser.getSettings().getDisplayZoomControls(), "Zoom buttons still enabled");
                } catch (Exception e) { throw new RuntimeException(e); }
            });
            checkInsets();
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
            runOnMainSync(() -> {root.removeAllViews();browser.destroy();browser=appBrowser;root.addView(browser,new LinearLayout.LayoutParams(-1,-1));});
            checkBackNavigation();
            result.putString("stream", "LOHO_ZOOM_PASS: flat page, four real client iframe tabs and user dialog stay at scale 1; provider-enabled guard passes; tap/scroll work; plain-WebView control scale=" + control + "\nLOHO_BACK_PASS: hardware Back closes top dialog, respects cancel, returns tabs to overview and exits root without replaying browser history\nLOHO_INSETS_PASS: bars/cutout/IME handled once and consumed; Android header/footer 48px with zero CSS safe-area padding\n");
            finish(Activity.RESULT_OK, result);
        } catch (Throwable error) {
            result.putString("stream", "LOHO_ZOOM_FAIL: " + android.util.Log.getStackTraceString(error));
            finish(Activity.RESULT_CANCELED, result);
        }
    }
    private static void require(boolean condition, String message) { if (!condition) throw new AssertionError(message); }
    private void checkInsets(){
        runOnMainSync(()->{
            WindowInsets original=root.getRootWindowInsets();
            int types=WindowInsets.Type.systemBars()|WindowInsets.Type.displayCutout()|WindowInsets.Type.ime();
            WindowInsets test=new WindowInsets.Builder().setInsets(WindowInsets.Type.systemBars(),Insets.of(11,29,13,19))
                .setInsets(WindowInsets.Type.displayCutout(),Insets.of(27,0,0,0)).setInsets(WindowInsets.Type.ime(),Insets.of(0,0,0,231)).setVisible(types,true).build();
            WindowInsets result=root.dispatchApplyWindowInsets(test);
            require(result.isConsumed(),"Insets forwarded to WebView after native padding");
            require(root.getPaddingLeft()==27&&root.getPaddingTop()==29&&root.getPaddingRight()==13&&root.getPaddingBottom()==231,"Bars, cutout or keyboard padding incorrect");
            root.dispatchApplyWindowInsets(new WindowInsets.Builder(test).setInsets(WindowInsets.Type.ime(),Insets.NONE).setVisible(WindowInsets.Type.ime(),false).build());
            require(root.getPaddingBottom()==19,"Keyboard hide left stale padding");
            if(original!=null)root.dispatchApplyWindowInsets(original);else root.requestApplyInsets();
        });
        waitForIdleSync();
    }
    private void screenshot() throws Exception {
        CountDownLatch painted=new CountDownLatch(1);
        runOnMainSync(()->browser.postVisualStateCallback(1,new WebView.VisualStateCallback(){
            @Override public void onComplete(long id){browser.invalidate();painted.countDown();}
        }));
        require(painted.await(5,TimeUnit.SECONDS),"WebView did not finish painting screenshot");
        waitForIdleSync();SystemClock.sleep(500);
        Bitmap bitmap=getUiAutomation().takeScreenshot();if(bitmap==null)return;
        try(FileOutputStream out=new FileOutputStream(new File(getTargetContext().getExternalFilesDir(null),"viewport.png"))){bitmap.compress(Bitmap.CompressFormat.PNG,100,out);}finally{bitmap.recycle();}
    }
    private void backKey(){sendKeyDownUpSync(KeyEvent.KEYCODE_BACK);waitForIdleSync();SystemClock.sleep(250);}
    private void checkBackNavigation() throws Exception {
        js("NodeViewClientHost.navigate('/node-view/client.html?tab=overview')");
        waitFor("!!document.querySelector('iframe:not([hidden])').contentDocument.querySelector('.user-card')");
        js("document.querySelector('iframe:not([hidden])').contentDocument.querySelector('.user-card').click();document.querySelector('.client-account').click()");
        backKey();
        require("true".equals(js("!document.querySelector('.client-tools-dialog').open && document.querySelector('iframe:not([hidden])').contentDocument.querySelector('.profile-dialog').open")),"Back did not close the topmost parent dialog first");
        js("document.querySelector('iframe:not([hidden])').contentDocument.querySelector('.profile-dialog').addEventListener('cancel',e=>e.preventDefault(),{once:true})");
        backKey();
        require("true".equals(js("document.querySelector('iframe:not([hidden])').contentDocument.querySelector('.profile-dialog').open")),"Back ignored cancellation while a dialog was busy");
        backKey();
        require("true".equals(js("!document.querySelector('iframe:not([hidden])').contentDocument.querySelector('.profile-dialog').open && NodeViewClientHost.current==='overview'")),"Back did not close user dialog");
        for(String route:new String[]{"connections","users","notifications"}){
            js("NodeViewClientHost.navigate('/node-view/client.html?tab="+route+"')");
            waitFor("NodeViewClientHost.current==='"+route+"'");backKey();
            require("true".equals(js("NodeViewClientHost.current==='overview'")),"Back did not return "+route+" to overview");
        }
        require(number("history.length")>1,"Test did not create prior tab history");
        backKey();
        for(int i=0;i<40;i++){
            java.util.concurrent.atomic.AtomicBoolean exited=new java.util.concurrent.atomic.AtomicBoolean();
            runOnMainSync(()->exited.set(activity.isFinishing()||activity.isDestroyed()));
            if(exited.get())return;SystemClock.sleep(100);
        }
        throw new AssertionError("Root Back replayed tab history instead of exiting Activity");
    }
    private void checkRealClient() throws Exception {
        runOnMainSync(() -> {
            browser.setWebChromeClient(new WebChromeClient(){
                @Override public boolean onConsoleMessage(ConsoleMessage message){diagnostics+=message.message()+" at "+message.sourceId()+":"+message.lineNumber()+"\n";return true;}
            });
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
                    }catch(Exception e){diagnostics+=request.getUrl()+": "+e+"\n";return new WebResourceResponse("text/plain","UTF-8",404,"Not found",null,new ByteArrayInputStream(new byte[0]));}
                }
            });
            browser.loadUrl("https://loho.invalid/node-view/client.html");
        });
        waitFor("!!window.NodeViewClientHost && !!document.querySelector('.client-nav')");
        require("true".equals(js("(()=>{const s=getComputedStyle(document.documentElement),h=document.querySelector('.client-header').getBoundingClientRect(),n=document.querySelector('.client-nav').getBoundingClientRect();return ['top','right','bottom','left'].every(x=>s.getPropertyValue('--client-safe-'+x).trim()==='0px')&&Math.abs(h.height-48)<.5&&Math.abs(n.height-48)<.5})()")),"Android chrome includes duplicate safe areas or excess height");
        String[] routes={"overview","connections","users","notifications"};
        String[] selectors={".user-card",".record",".account","#topic"};
        for(int i=0;i<routes.length;i++){
            js("NodeViewClientHost.navigate('/node-view/client.html?tab="+routes[i]+"')");
            waitFor("!!document.querySelector('iframe:not([hidden])')?.contentDocument?.querySelector('"+selectors[i]+"')");
            if(i==0)screenshot();
            // Remove the CSS fallback so this specifically proves the native guard.
            js("document.documentElement.style.setProperty('touch-action','auto','important');document.querySelector('iframe:not([hidden])').contentDocument.documentElement.style.setProperty('touch-action','auto','important')");
            pinch();
            require(Math.abs(number("visualViewport.scale")-1)<.02,"Root zoomed on "+routes[i]);
            require(Math.abs(number("document.querySelector('iframe:not([hidden])').contentWindow.visualViewport.scale")-1)<.02,"Iframe zoomed on "+routes[i]);
            if(i==1)require("true".equals(js("(()=>{const d=document.querySelector('iframe:not([hidden])').contentDocument,w=d.defaultView,s=['search','user','source','window'].map(id=>w.getComputedStyle(d.getElementById(id)));return s.every(x=>x.fontSize==='13px'&&x.fontWeight==='400')&&new Set(s.map(x=>x.fontFamily)).size===1})()")),"Android filter fonts differ");
            if(i==0){
                js("document.querySelector('iframe:not([hidden])').contentDocument.querySelector('.user-card').click()");pinch();
                require(Math.abs(number("visualViewport.scale")-1)<.02,"User dialog zoomed");
                js("document.querySelector('iframe:not([hidden])').contentDocument.querySelector('.profile-dialog').close()");
                waitFor("!document.querySelector('iframe:not([hidden])').contentDocument.querySelector('.profile-dialog').open");
            }
        }
    }
    private void waitFor(String expression) throws Exception {
        for(int i=0;i<150;i++){if("true".equals(js(expression))){SystemClock.sleep(300);return;}SystemClock.sleep(100);}
        throw new AssertionError("Client did not become ready: "+expression+"; document="+js("JSON.stringify({url:location.href,html:document.documentElement.outerHTML.slice(0,500),text:document.body?.innerText.slice(0,200),host:!!window.NodeViewClientHost})")+"; console="+diagnostics);
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
