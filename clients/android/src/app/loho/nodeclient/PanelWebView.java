package app.loho.nodeclient;

import android.content.Context;
import android.view.MotionEvent;
import android.webkit.WebView;

/** Keep a multi-finger gesture out of the WebView provider and embedded pages. */
public final class PanelWebView extends WebView {
    private boolean multiTouch;

    public PanelWebView(Context context) { super(context); }

    @Override public boolean dispatchTouchEvent(MotionEvent event) {
        int action = event.getActionMasked();
        if (action == MotionEvent.ACTION_DOWN) multiTouch = false;
        if (event.getPointerCount() > 1 || multiTouch) {
            if (!multiTouch) {
                MotionEvent cancel = MotionEvent.obtain(event);
                cancel.setAction(MotionEvent.ACTION_CANCEL);
                try { super.dispatchTouchEvent(cancel); } finally { cancel.recycle(); }
                multiTouch = true;
            }
            if (action == MotionEvent.ACTION_UP || action == MotionEvent.ACTION_CANCEL) multiTouch = false;
            return true;
        }
        return super.dispatchTouchEvent(event);
    }
}
