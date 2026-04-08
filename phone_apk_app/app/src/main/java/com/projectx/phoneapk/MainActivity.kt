package com.projectx.phoneapk

import android.Manifest
import android.annotation.SuppressLint
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.webkit.PermissionRequest
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.ProgressBar
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.content.ContextCompat.startForegroundService
import android.content.pm.PackageManager

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var progressBar: ProgressBar

    private var pendingPermissionRequest: PermissionRequest? = null
    private var pendingWebResources: Array<String> = emptyArray()

    private val runtimePermissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { result ->
            val grantedAll = result.values.all { it }
            val request = pendingPermissionRequest
            if (request != null) {
                if (grantedAll) {
                    request.grant(pendingWebResources)
                } else {
                    request.deny()
                }
            }
            pendingPermissionRequest = null
            pendingWebResources = emptyArray()
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        startKeepAliveService()

        webView = findViewById(R.id.phoneWebView)
        progressBar = findViewById(R.id.pageProgress)

        configureWebView()
        ensureBasePermissions()
        loadPhonePage()
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun configureWebView() {
        val settings: WebSettings = webView.settings
        settings.javaScriptEnabled = true
        settings.domStorageEnabled = true
        settings.databaseEnabled = true
        settings.mediaPlaybackRequiresUserGesture = false
        settings.allowFileAccess = false
        settings.allowContentAccess = false
        settings.cacheMode = WebSettings.LOAD_DEFAULT
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            settings.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            settings.safeBrowsingEnabled = true
        }

        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
                val scheme = request?.url?.scheme?.lowercase() ?: return true
                return scheme != "http" && scheme != "https"
            }
        }

        webView.webChromeClient = object : WebChromeClient() {
            override fun onProgressChanged(view: WebView?, newProgress: Int) {
                progressBar.progress = newProgress
                progressBar.visibility = if (newProgress >= 100) ProgressBar.GONE else ProgressBar.VISIBLE
            }

            override fun onPermissionRequest(request: PermissionRequest) {
                runOnUiThread {
                    handleWebPermissionRequest(request)
                }
            }
        }
    }

    private fun loadPhonePage() {
        val prefs = getSharedPreferences("phone_apk_prefs", MODE_PRIVATE)
        val savedUrl = prefs.getString("phone_url", null)

        if (savedUrl.isNullOrBlank() || !isSafeWebUrl(savedUrl)) {
            promptForUrl(BuildConfig.PHONE_PAGE_URL)
        } else {
            webView.loadUrl(savedUrl)
        }
    }

    private fun promptForUrl(defaultUrl: String) {
        val input = EditText(this)
        input.setText(defaultUrl)
        input.hint = "https://your-domain.example/phone"

        AlertDialog.Builder(this)
            .setTitle(getString(R.string.set_server_url))
            .setMessage(getString(R.string.set_server_url_message))
            .setView(input)
            .setCancelable(false)
            .setPositiveButton(getString(R.string.save_and_open)) { _, _ ->
                val url = input.text.toString().trim()
                if (isSafeWebUrl(url)) {
                    getSharedPreferences("phone_apk_prefs", MODE_PRIVATE)
                        .edit()
                        .putString("phone_url", url)
                        .apply()
                    webView.loadUrl(url)
                } else {
                    promptForUrl(defaultUrl)
                }
            }
            .show()
    }

    private fun isSafeWebUrl(value: String?): Boolean {
        val candidate = value?.trim().orEmpty()
        if (candidate.isBlank()) return false

        val uri = try {
            Uri.parse(candidate)
        } catch (_: Exception) {
            return false
        }

        val scheme = uri.scheme?.lowercase() ?: return false
        return scheme == "http" || scheme == "https"
    }

    private fun ensureBasePermissions() {
        val needed = mutableListOf<String>()

        if (!isGranted(Manifest.permission.CAMERA)) {
            needed += Manifest.permission.CAMERA
        }
        if (!isGranted(Manifest.permission.RECORD_AUDIO)) {
            needed += Manifest.permission.RECORD_AUDIO
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU && !isGranted(Manifest.permission.POST_NOTIFICATIONS)) {
            needed += Manifest.permission.POST_NOTIFICATIONS
        }

        if (needed.isNotEmpty()) {
            runtimePermissionLauncher.launch(needed.toTypedArray())
        }
    }

    private fun startKeepAliveService() {
        val serviceIntent = Intent(this, BackgroundKeepAliveService::class.java)
        startForegroundService(this, serviceIntent)
    }

    private fun isGranted(permission: String): Boolean {
        return ContextCompat.checkSelfPermission(this, permission) == PackageManager.PERMISSION_GRANTED
    }

    private fun handleWebPermissionRequest(request: PermissionRequest) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) {
            request.grant(request.resources)
            return
        }

        val requiredRuntimePermissions = mutableSetOf<String>()
        val resources = request.resources

        for (res in resources) {
            when (res) {
                PermissionRequest.RESOURCE_VIDEO_CAPTURE -> requiredRuntimePermissions += Manifest.permission.CAMERA
                PermissionRequest.RESOURCE_AUDIO_CAPTURE -> requiredRuntimePermissions += Manifest.permission.RECORD_AUDIO
            }
        }

        val notGranted = requiredRuntimePermissions.filterNot { isGranted(it) }
        if (notGranted.isEmpty()) {
            request.grant(resources)
            return
        }

        pendingPermissionRequest = request
        pendingWebResources = resources
        runtimePermissionLauncher.launch(notGranted.toTypedArray())
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack()
        } else {
            super.onBackPressed()
        }
    }

    override fun onDestroy() {
        webView.stopLoading()
        webView.webChromeClient = null
        webView.webViewClient = WebViewClient()
        webView.destroy()
        super.onDestroy()
    }
}
