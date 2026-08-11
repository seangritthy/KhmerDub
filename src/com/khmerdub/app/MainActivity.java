package com.khmerdub.app;

import android.Manifest;
import android.app.Activity;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.InputStream;
import java.util.UUID;

public class MainActivity extends Activity {

    private WebView webView;
    private WebView snifferWebView;
    private DownloadService downloadService;
    private Handler mainHandler = new Handler(Looper.getMainLooper());
    private String pendingShareUrl = null;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        downloadService = new DownloadService(this);

        requestPermissionsIfNeeded();

        webView = findViewById(R.id.webview);
        snifferWebView = findViewById(R.id.sniffer_webview);

        setupWebViews();

        handleIncomingIntent(getIntent());

        webView.loadUrl("file:///android_asset/index.html");
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        handleIncomingIntent(intent);
    }

    private void handleIncomingIntent(Intent intent) {
        if (intent != null && Intent.ACTION_SEND.equals(intent.getAction()) && "text/plain".equals(intent.getType())) {
            String sharedText = intent.getStringExtra(Intent.EXTRA_TEXT);
            if (sharedText != null && (sharedText.startsWith("http://") || sharedText.startsWith("https://"))) {
                pendingShareUrl = sharedText;
                if (webView != null) {
                    mainHandler.postDelayed(new Runnable() {
                        @Override
                        public void run() {
                            webView.evaluateJavascript("window.onSharedUrlReceived && window.onSharedUrlReceived('" + pendingShareUrl.replace("'", "\\'") + "')", null);
                        }
                    }, 1000);
                }
            }
        }
    }

    private void requestPermissionsIfNeeded() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            if (checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
                requestPermissions(new String[]{
                        Manifest.permission.WRITE_EXTERNAL_STORAGE,
                        Manifest.permission.READ_EXTERNAL_STORAGE
                }, 101);
            }
        }
    }

    private void setupWebViews() {
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(true);
        settings.setAllowContentAccess(true);
        settings.setAllowFileAccessFromFileURLs(true);
        settings.setAllowUniversalAccessFromFileURLs(true);
        settings.setDatabaseEnabled(true);
        settings.setMediaPlaybackRequiresUserGesture(false);

        webView.setWebChromeClient(new WebChromeClient());
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                super.onPageFinished(view, url);
                if (pendingShareUrl != null) {
                    webView.evaluateJavascript("window.onSharedUrlReceived && window.onSharedUrlReceived('" + pendingShareUrl.replace("'", "\\'") + "')", null);
                    pendingShareUrl = null;
                }
            }

            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                if (request != null && request.getUrl() != null) {
                    android.net.Uri uri = request.getUrl();
                    String host = uri.getHost();
                    String scheme = uri.getScheme();
                    if ("localfile".equals(host) || "localfile".equals(scheme)) {
                        try {
                            String filePath = uri.getPath();
                            if (filePath != null) {
                                java.io.File file = new java.io.File(filePath);
                                if (file.exists()) {
                                    String mime = filePath.toLowerCase().endsWith(".mp3") ? "audio/mpeg" : "video/mp4";
                                    java.io.FileInputStream fis = new java.io.FileInputStream(file);
                                    java.util.Map<String, String> headers = new java.util.HashMap<>();
                                    headers.put("Access-Control-Allow-Origin", "*");
                                    headers.put("Accept-Ranges", "bytes");
                                    return new WebResourceResponse(mime, "UTF-8", 200, "OK", headers, fis);
                                }
                            }
                        } catch (Exception e) {
                            e.printStackTrace();
                        }
                    }
                }
                return super.shouldInterceptRequest(view, request);
            }
        });

        webView.addJavascriptInterface(new AndroidBridge(), "AndroidBridge");

        // Sniffer WebView setup
        WebSettings snifferSettings = snifferWebView.getSettings();
        snifferSettings.setJavaScriptEnabled(true);
        snifferSettings.setDomStorageEnabled(true);
        snifferSettings.setUserAgentString("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36");

        snifferWebView.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                if (request != null && request.getUrl() != null) {
                    String url = request.getUrl().toString();
                    if (url.contains(".mp4") || url.contains(".m3u8") || url.contains(".webm") || url.contains("video")) {
                        notifySniffedMedia(url);
                    }
                }
                return super.shouldInterceptRequest(view, request);
            }
        });
    }

    private void notifySniffedMedia(final String mediaUrl) {
        mainHandler.post(new Runnable() {
            @Override
            public void run() {
                if (webView != null) {
                    webView.evaluateJavascript("window.onMediaSniffed && window.onMediaSniffed('" + mediaUrl.replace("'", "\\'") + "')", null);
                }
            }
        });
    }

    private class AndroidBridge {

        @JavascriptInterface
        public void parseUrl(final String urlStr) {
            new Thread(new Runnable() {
                @Override
                public void run() {
                    final VideoExtractor.VideoItem item = VideoExtractor.extract(urlStr);
                    mainHandler.post(new Runnable() {
                        @Override
                        public void run() {
                            JSONObject json = item.toJsonObject();
                            webView.evaluateJavascript("window.onParseResult && window.onParseResult(" + json.toString() + ")", null);
                        }
                    });
                }
            }).start();
        }

        @JavascriptInterface
        public void startDownload(final String videoUrl, final String title, final String format) {
            startDownload(videoUrl, title, format, videoUrl);
        }

        @JavascriptInterface
        public void startDownload(final String videoUrl, final String title, final String format, final String pageUrl) {
            final String downloadId = UUID.randomUUID().toString();
            downloadService.startDownload(downloadId, videoUrl, title, format, pageUrl, new DownloadService.DownloadListener() {
                @Override
                public void onProgress(final String id, final int progress, final long bytesDownloaded, final long totalBytes, final String status) {
                    mainHandler.post(new Runnable() {
                        @Override
                        public void run() {
                            webView.evaluateJavascript("window.onDownloadProgress && window.onDownloadProgress('" + id + "', " + progress + ", " + bytesDownloaded + ", " + totalBytes + ", '" + status + "')", null);
                        }
                    });
                }

                @Override
                public void onComplete(final String id, final String filePath) {
                    mainHandler.post(new Runnable() {
                        @Override
                        public void run() {
                            webView.evaluateJavascript("window.onDownloadComplete && window.onDownloadComplete('" + id + "', '" + filePath.replace("'", "\\'") + "')", null);
                            Toast.makeText(MainActivity.this, "Download Complete!", Toast.LENGTH_SHORT).show();
                        }
                    });
                }

                @Override
                public void onError(final String id, final String error) {
                    mainHandler.post(new Runnable() {
                        @Override
                        public void run() {
                            webView.evaluateJavascript("window.onDownloadError && window.onDownloadError('" + id + "', '" + error.replace("'", "\\'") + "')", null);
                            Toast.makeText(MainActivity.this, "Error: " + error, Toast.LENGTH_SHORT).show();
                        }
                    });
                }
            });
        }

        @JavascriptInterface
        public String getClipboardText() {
            try {
                ClipboardManager clipboard = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
                if (clipboard.hasPrimaryClip() && clipboard.getPrimaryClip().getItemCount() > 0) {
                    CharSequence text = clipboard.getPrimaryClip().getItemAt(0).getText();
                    return text != null ? text.toString() : "";
                }
            } catch (Exception e) {
                e.printStackTrace();
            }
            return "";
        }

        @JavascriptInterface
        public String getDownloadedFiles() {
            JSONArray files = downloadService.getDownloadedFiles();
            return files.toString();
        }

        @JavascriptInterface
        public boolean deleteFile(String path) {
            return downloadService.deleteFile(path);
        }

        @JavascriptInterface
        public void openFile(String path) {
            downloadService.openFile(path);
        }

        @JavascriptInterface
        public void loadSnifferUrl(final String url) {
            mainHandler.post(new Runnable() {
                @Override
                public void run() {
                    snifferWebView.loadUrl(url);
                }
            });
        }

        @JavascriptInterface
        public String getAppVersion() {
            try {
                return "v" + getPackageManager().getPackageInfo(getPackageName(), 0).versionName + " Pro Edition";
            } catch (Exception e) {
                return "v1.0.0 Pro Edition";
            }
        }

        @JavascriptInterface
        public void installApk(String path) {
            downloadService.installApk(path);
        }

        @JavascriptInterface
        public void showToast(String message) {
            Toast.makeText(MainActivity.this, message, Toast.LENGTH_SHORT).show();
        }
    }
}
